# AGY Provider Contract Captures (Gate 0, plan `agy-full-integration` §1.1)

Captured 2026-08-22 by task #19563 in **both** print mode (`agy -p … --output-format
stream-json`) and interactive terminal mode (raw tmux, plan §1.1 mechanics). Two runs:
the full both-mode probe (03:17–04:30 local) and the **daemon-receipt run** (05:40–05:50
local, 10:40–10:50Z) that re-ran the three fixed prompts per mode with the real `ghook`
delivering every hook event to the daemon.

**Version.** The first run started on the installed AGY **1.1.16** (the two `/hooks`
captures at 03:17–03:18 local) and AGY's auto-updater replaced `~/.local/bin/agy` with
**1.1.18** at 03:18:34, before the first live turn. Every live record below was
observed on **1.1.18** (`agy --version` → `1.1.18`; the interactive banner reads
`Antigravity CLI 1.1.18`, `pane-captures/1.1.7-interactive-startup.txt`). There is no
pin or downgrade path, so the probed floor is 1.1.18.

**Probe environment.** The agent session ran under an SRT sandbox whose proxy forbids
Google domains, so every AGY invocation was executed in an outside tmux pane
(`tmux new-session … ; tmux send-keys …`), which is also the plan's terminal-mode
mechanism. The Gemini weekly bucket hit 0 % mid-run (`Individual quota reached`), so
later turns — including the whole receipt run — used `--model gpt-oss-120b-medium`
(separate bucket); records note the model where it matters.

## Files

| File | Records | Content |
| --- | --- | --- |
| `hook-payloads.jsonl` | 1.1.3, 1.1.5, 1.1.17, 1.1.24 | 76 live camelCase payloads with `mode` (`print` / `interactive`), hook cwd, env, the capture hook's answer and exit code; the 49 receipt-run lines carry `envelope_id`, which joins them to `daemon-receipts.jsonl` |
| `daemon-receipts.jsonl` | 1.1.5, 1.1.17 | 49 daemon-side receipts, one per `ghook` delivery of the receipt run: `mode`, `event`, `tool_class` (`built-in` / `shell` / `mcp`), the daemon's HTTP status + response body, the daemon's processed-envelope marker (`~/.gobby/hooks/inbox/processed/<sha256(envelope_id)>.json`), and the matching `~/.gobby/logs/hooks.log` line |
| `pane-captures/<record>-interactive[-<label>].txt` | 1.1.3, 1.1.5, 1.1.7, 1.1.8, 1.1.14, 1.1.17, 1.1.23 | scrubbed `tmux capture-pane -p -S -200 -t agy-gate0` output, one file per cited terminal-mode observation |
| `transcript-manifest.json` | 1.1.2, 1.1.10, 1.1.22 | transcript layout, literal `transcriptPath`, record census, zero/nonzero-exit shell records, truncation evidence |
| `stream-json-samples.jsonl` | 1.1.1, 1.1.6, 1.1.8, 1.1.13, 1.1.18, 1.1.20 | scrubbed NDJSON records (init, resumed turn, text_delta, tool ACTIVE/DONE/ERROR, failure results, stream-input errors, synthetic malformed line) |
| `command-captures.json` | 1.1.7, 1.1.13, 1.1.15, 1.1.19, 1.1.20, 1.1.21 | `/hooks` before/with/after the capture hook (both runs), `/usage` `/quota` `/credits` `/model`, `models`/`agents` JSON, isolated-HOME `models`, `mcp list`, flag-syntax errors, auth probes |

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
   directory`), shell (`run: ls -la`), MCP (`call the gobby list_mcp_servers tool and
   report the result`), plus the targeted probes per record below. 566 hook
   invocations captured in the first run.
3. Interactive: `tmux new-session -d -s agy-gate0 -x 200 -y 50 -c <ws> "agy
   --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium
   --add-dir <ws>"`; prompt glyph `>` on its own line between two horizontal rules,
   status line `? for shortcuts … <model label>`; the same three prompts, `shift+tab`,
   `/plan`, `ctrl+r`, `C-c`; a second session without the two flags for the native
   permission prompt. `tmux kill-session` and orphan check at the end.
4. **Daemon-receipt run.** `gate0-capture.sh` additionally pipes the same stdin into
   the real hook binary, `GOBBY_PROJECT_ID=<PROJECT_ID> GOBBY_DAEMON_URL=http://127.0.0.1:60899
   ~/.gobby/bin/ghook --gobby-owned --cli=agy --type=<Event>`, where `:60899` is a
   loopback recording proxy (`receipt_proxy.py`) in front of the daemon's
   `:60887`. The proxy logs the `X-Gobby-Envelope-Id` header, the envelope, and the
   daemon's HTTP status + body; afterwards the daemon's processed marker for each
   envelope id and the `hooks.log` line written at delivery time are joined in.
   `GOBBY_PROJECT_ID` is what a Gobby-spawned AGY carries; without it (or a
   `.gobby/project.json` under a `workspacePaths` entry) `ghook` treats the run as
   unmanaged and answers the skip JSON without posting — that is why the first run's
   `gobby` hook left only one `hooks.log` line. A first attempt that set
   `GOBBY_PROJECT_ID` in AGY's own environment made the installed `gobby` hook managed
   too, and the daemon's `PreInvocation` `injectSteps.ephemeralMessage` (skill-loading
   rules) derailed the model into MCP calls (`result ERROR … tool list_resources is not
   enabled for server gobby-skills`); the committed run scopes the variable to the
   capture hook's `ghook` only.
5. Remove `gate0-capture`; `/hooks` after shows only `gobby` (5 actions); `hooks.json`
   is byte-equivalent to the pre-probe backup (both runs).
6. Scrub: `$HOME`→`~`, conversation ids→`<CONVERSATION_ID>`, workspace→`<WORKSPACE>`,
   probe scratch→`<PROBE_SCRATCH>`, app-data paths other than `brain/<id>/…`→
   `<AGY_APP_DATA>/…`, the local user name→`<USER>`, project id→`<PROJECT_ID>`,
   emails/tokens→`<REDACTED>`, tool output >4 KiB truncated with `<TRUNCATED n bytes>`.
   `ghook` envelope ids (`n-<ms>-<uuid>`) are kept: they key the daemon markers and are
   not conversation ids.

## Contract-outcome table (1.1.11)

Outcomes: **confirmed** (open record answered as the plan assumed), **re-confirmed
unchanged** (1.1.10 record identical on 1.1.18), **disproven** (observed behaviour
contradicts plan text; the affected sections were revised), **negative** (the
capability is absent; recorded as a negative contract). Every row's literal command
and observed output is in "Record evidence" below.

| Record | Outcome | Summary |
| --- | --- | --- |
| 1.1.1 resume | **re-confirmed unchanged** (+1 delta) | `--conversation <id>` resumes: same id, `num_turns` 1→2, prior turn recalled, also after SIGINT/SIGTERM. Delta: `duration_seconds` is cumulative per conversation. |
| 1.1.2 transcriptPath | **re-confirmed, layout disproven** | Literal value `~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl` in both modes; no workspace-local file. |
| 1.1.3 cwd remedy | **confirmed, remedy named** | Unregistered cwd → `workspacePaths []`, tools target app data. Remedy: `--add-dir <cwd>` on every launch. |
| 1.1.4 image input | **negative** | No image flag; `@path` is plain text; stream-input image blocks rejected; only the model's own `view_file` sees an image. |
| 1.1.5 payloads | **confirmed (hook→daemon receipts in both modes)** | Five camelCase events per mode for a built-in, a shell and an MCP tool; 49 daemon receipts (HTTP 200 + marker + `hooks.log` line). `source=agy` line / session row are §4.1's acceptance. |
| 1.1.6 stream-json | **re-confirmed unchanged** (+2 deltas) | Nested `{"event":…,"<event>":{…}}`; 57 tools; `CANCELED` status; no >64 KiB sample (AGY caps at ~8 KiB). |
| 1.1.7 sandbox flags | **re-confirmed unchanged** | `--sandbox` boolean, `--sandbox=false` accepted both modes; skip flag → `always-proceed`; without it headless tools auto-deny. |
| 1.1.8 cancellation | **confirmed** | SIGINT/SIGTERM exit 1 with the timeout payload, shell child orphaned, resume works; terminal `C-c` interrupts without `Stop`. |
| 1.1.9 network/roots | **confirmed** | Google API/OAuth/telemetry hosts plus Playwright CDN; app-data roots enumerated below. |
| 1.1.10 RUN_COMMAND | **disproven** | Both exit classes are `MODEL/GENERIC` free text; no `RUN_COMMAND` record, no structured `exit_code`. |
| 1.1.11 outcome table | **confirmed** | This table. |
| 1.1.12 controlled-tool bridge | **confirmed (supported)** | `PreToolUse` `decision:"deny"` transport; MCP tools surface as `call_mcp_tool`. |
| 1.1.13 `--print-timeout` | **re-confirmed unchanged** (+1 delta) | Go syntax, default `5m0s`, no disable sentinel, expiry exit 1; under `json|stream-json` a stdout `result{status:ERROR}`. |
| 1.1.14 terminal plan menu | **confirmed** | `shift+tab` cycles modes; `ctrl+r`/`/artifact` review with `y`/`n`/`shift+a`/`p`/`esc`; permission prompt `1`–`4`/`esc`. |
| 1.1.15 auth footprint | **confirmed** | Keychain item `svce=gemini acct=antigravity`; env API-key vars ignored; foreign `HOME` → OAuth prompt, exit 1. |
| 1.1.16 compaction | **negative** | No compaction/context-pressure record; `checkpoint` fires at step 1 of every conversation. |
| 1.1.17 interactive dispatch | **confirmed** | All five events fire interactively with key sets identical to print mode; negatives apply to both modes. |
| 1.1.18 `--input-format stream-json` | **confirmed** | One `result` per turn; EOF → exit 0; per-turn timeout; `--conversation` accepted; SIGINT kills the process (`context canceled`, exit 1). |
| 1.1.19 usage/quota | **confirmed; `/credits` negative** | `/usage` shape recorded; `/quota` aliases it; `/credits` exit 1; exhausted = `remaining_fraction 0` + turn `result ERROR`. |
| 1.1.20 models | **disproven (placement), shape confirmed** | `agy models --output-format json` exit 1; `agy --output-format json models` → `models[].{id,label}`; unauthenticated (isolated HOME) exit 1, no prompt. |
| 1.1.21 `/hooks` | **confirmed** | `hooks[].{name,enabled,source,actions[]}`; disabled → `enabled:false`; malformed shows without warning; no agent turn. |
| 1.1.22 transcript layout | **confirmed** | Parser input `transcript_full.jsonl`; `transcript.jsonl` truncated twin; `chunks/` byte-identical copies. |
| 1.1.23 `--mode` | **confirmed** | Headless `plan` writes `brain/<id>/<name>.md`, no approval record; `accept-edits` writes without prompting; `bogus` → warning. |
| 1.1.24 response fields | **confirmed with negatives** | Honored: `deny`, `deny_unless_prior_grant`, `overwrite`, `terminationBehavior`, `injectSteps.userMessage`/`ephemeralMessage`, `Stop continue` ×10. Not honored: `permissionOverrides` (headless), `injectSteps.toolCall`. PreToolUse exit 1/2 blocks the tool; Stop exit 2 ignored. |

## Record evidence

Print-mode commands ran in `<WORKSPACE>` through the outside pane; `$F` below stands
for the common flag set `--output-format stream-json --sandbox=false
--dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE>`
(`--model gpt-oss-120b-medium` added after the Gemini bucket was exhausted). Stream
excerpts keep the record's exact JSON with `conversation_id` and `usage` elided.

### 1.1.1 — resume (`--conversation`)

```
$ agy -p 'what shell command did you run earlier in this conversation? answer in one line' $F --conversation <CONVERSATION_ID>
{"event":"step_update","step_update":{"step_index":6,"state":"DONE","step_type":"system_message","duration_seconds":0.000681}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"I ran `ls -la` earlier in this conversation.\n","duration_seconds":291.375441,"num_turns":2}}
exit 0
```

Same `conversation_id` as the `run: ls -la` turn, `num_turns` 1→2. After SIGINT
(1.1.8): `agy -p 'what command were you running before? one line' --output-format json … --conversation <CONVERSATION_ID>` →
`{"status":"SUCCESS","response":"The command I ran was:\n\n```bash\nsleep 40; echo finished-after-sleep\n```\n","num_turns":2}` exit 0.
Delta: `date +%s; agy -p 'reply with exactly: ok2' --output-format json … --conversation <CONVERSATION_ID>; date +%s` →
`1787388425` … `"duration_seconds":213.192009,"num_turns":2` … `1787388430` — 5 s of
wall clock reported as 213 s, i.e. measured from conversation creation.

### 1.1.2 — literal `transcriptPath`

From the first `PreInvocation` of every conversation, both modes (`hook-payloads.jsonl`, `transcript-manifest.json`):

```
"transcriptPath": "~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl"
"artifactDirectoryPath": "~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>"
"invocationNum": 0, "initialNumSteps": 1
```

`find <WORKSPACE> -name 'transcript*'` after every turn → no output: no
workspace-local transcript is ever created.

### 1.1.3 — cwd remedy

Unregistered cwd, no `--add-dir` (first run, `hook-payloads.jsonl` record `1.1.3`):

```
$ agy -p "list the files in this directory" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m
{"event":"init","init":{"cwd":"<WORKSPACE>","permission_mode":"always-proceed",…}}
{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"list_dir","tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"~/.gemini/antigravity-cli"}}}}
{"event":"step_update","step_update":{"step_index":3,"state":"ERROR",…"error":{"type":"TOOL_ERROR","message":"permission check failed for read_file \"~/.gemini/antigravity-cli\": Permission denied for read_file(~/.gemini/antigravity-cli). Matches hardcoded system protection boundary rule."}}}
{"event":"step_update","step_update":{"step_index":5,"state":"DONE","step_type":"tool","tool_name":"list_dir","tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"<AGY_APP_DATA>/scratch"},"output":".antigravitycli/"}}}
PreToolUse payload: "workspacePaths": []   (run_command carries no "Cwd")
```

Remedy (`--add-dir`), and the alternatives:

```
$ agy -p 'run: pwd' $F                     # $F includes --add-dir <WORKSPACE>
{"event":"step_update","step_update":{"step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"pwd"},"output":"<WORKSPACE>\n"}}}
PreToolUse payload: "workspacePaths": ["<WORKSPACE>"], "toolCall":{"args":{"CommandLine":"pwd","Cwd":"<WORKSPACE>",…}}
$ agy -p 'run: pwd' … --project <PROJECT_UUID>   → output "<WORKSPACE>\n" (needs the id from ~/.gemini/config/projects/<uuid>.json)
$ agy -p "run: ls -la" … --new-project          → works; writes a new ~/.gemini/config/projects/<uuid>.json per launch
```

Interactive: `pane-captures/1.1.3-interactive.txt` — `ListDir(<WORKSPACE>)` under
`--add-dir <WORKSPACE>`.

### 1.1.4 — image input

```
$ agy --help | grep -i image      → (no output)
$ agy -p 'describe the image file img.png in this directory: what color is it and what size?' $F
… "tool_name":"view_file","tool_info":{"name":"view_file","parameters":{"AbsolutePath":"<WORKSPACE>/img.png"}} …
{"event":"result","result":{"status":"SUCCESS","response":"… * **Color**: Solid red\n* **Dimensions**: 64 x 64 pixels\n* **File Size**: 168 bytes\n"}}
$ agy -p '@img.png what color is this image?' $F
… "tool_name":"find_by_name","parameters":{"Pattern":"*img.png*"} … "tool_name":"view_file" … python3 -c "from PIL import Image …" (ModuleNotFoundError) … PNG chunk parser …
{"event":"result","result":{"status":"SUCCESS","response":"The image [img.png](file://<WORKSPACE>/img.png) is **pure red** (RGB: `[255, 0, 0]`, Hex: `#FF0000`).\n"}}
--input-format stream-json: {"event":"user","message":{"content":[{"type":"image","source":"x"}]}}
{"event":"result","result":{"status":"ERROR","error":"stream input content block type \"image\" is not supported (only \"text\")"}}  exit 1
```

`@path` is plain text (the model searched for it); the only image path is the model's
own `view_file`, so `VISION_EXTRACT` stays unavailable as a Gobby binding.

### 1.1.5 — live camelCase payloads and daemon receipts

Receipt run, print mode (three conversations, `hook-payloads.jsonl` lines with
`envelope_id`, `daemon-receipts.jsonl`):

```
$ agy -p 'list the files in this directory' $F --model gpt-oss-120b-medium       # built-in: list_dir
$ agy -p 'run: ls -la' $F --model gpt-oss-120b-medium                           # shell: run_command
$ agy -p 'call the gobby list_mcp_servers tool and report the result' $F --model gpt-oss-120b-medium   # mcp: call_mcp_tool
```

Per turn the capture hook received, in order, `PreInvocation(invocationNum 0)`,
`PreToolUse(stepIdx 3)`, `PostToolUse(stepIdx 3)`, `PostInvocation`,
`PreInvocation(invocationNum 1)`, `PostInvocation`, `Stop(terminationReason
NO_TOOL_CALL)`. Key sets: common `artifactDirectoryPath, conversationId, modelName,
transcriptPath, workspacePaths`; Pre/PostInvocation `+invocationNum, initialNumSteps`;
Pre/PostToolUse `+stepIdx, toolCall{name,args}` (`+error` on PostToolUse); Stop
`+executionNum, terminationReason, error, fullyIdle`. Hook cwd `~/.gemini/config`;
env `ANTIGRAVITY_CONVERSATION_ID=<CONVERSATION_ID>`.

Daemon receipt for the print-mode shell `PreToolUse` (`daemon-receipts.jsonl`, abridged):

```
{"mode":"print","event":"PreToolUse","tool_class":"shell","tool_name":"run_command",
 "envelope_id":"n-1787395543…","ghook_command":"ghook --gobby-owned --cli=agy --type=PreToolUse",
 "ghook_request":{"method":"POST","path":"/api/hooks/execute","headers":{"X-Gobby-Project-Id":"<PROJECT_ID>","X-Gobby-Envelope-Id":"n-1787395543…","Authorization":"<REDACTED>"}},
 "payload":{"schema_version":1,"critical":false,"hook_type":"PreToolUse","source":"agy","input_data_keys":["artifactDirectoryPath","conversationId","machine_id","modelName","os","stepIdx","toolCall","transcriptPath","workspacePaths"]},
 "daemon_http_status":200,"daemon_response":{"decision":"allow"},
 "daemon_processed_marker":{"envelope_id":"n-1787395543…","processed_at":"2026-08-22T10:45:43.…+00:00","response":{"decision":"allow"},"status":"processed"},
 "daemon_hooks_log_line":"2026-08-22 05:45:43 - WARNING - hooks.broadcaster.broadcast_event - Failed to broadcast event HookEventType.BEFORE_TOOL: 2 validation errors for PreToolUseInput",
 "ghook_stdout":{"decision":"allow"},"ghook_exit":0}
```

Coverage (`daemon-receipts.jsonl`, all `daemon_http_status` 200, all with a marker
and a `hooks.log` line): print × {built-in, shell, mcp} × {PreInvocation, PreToolUse,
PostToolUse, PostInvocation, Stop}; interactive × the same three tool classes (turns 0–2
of one session) × the same five events, plus the interrupted turn 3 (PreInvocation,
PreToolUse `run_command`, PostInvocation, no Stop). Daemon responses: `PreInvocation` →
`{"injectSteps":[{"ephemeralMessage":"Load and fully read the skill …"}]}` (the
daemon's rule engine), `PreToolUse` → `{"decision":"allow"}`, `PostToolUse`/
`PostInvocation` → `{}`, `Stop` → `{"decision":"continue","reason":"Rule enforced by
Gobby: [require-…"}`. The `hooks.log` lines are the daemon's validation warnings
(`PreToolUseInput` wants `tool_name`, `UserPromptSubmitInput` wants `external_id` /
`prompt_text`, `StopInput` …): the pre-§4.1 adapter reads snake_case keys, which is
exactly the aliasing §4.1 delivers; the `source=agy` line and the AGY session row are
§4.1's acceptance 4.1.22.

Interactive: `pane-captures/1.1.5-interactive-{builtin,shell,mcp}.txt`.

### 1.1.6 — stream-json shape

```
$ agy -p 'list the files in this directory' $F
{"event":"init","conversation_id":"<CONVERSATION_ID>","init":{"model":"gpt-oss-120b-medium","cwd":"<WORKSPACE>","tools":[…57 entries…],"permission_mode":"always-proceed",…}}
{"event":"step_update","step_update":{"step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"step_index":1,"state":"DONE","step_type":"unknown","duration_seconds":0.000109}}
{"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"checkpoint","duration_seconds":0.954066}}
{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"list_dir","tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"<WORKSPACE>"}}}}
{"event":"step_update","step_update":{"step_index":3,"state":"DONE","step_type":"tool","tool_name":"list_dir","duration_seconds":0.360846,"tool_info":{…,"output":"img.png\nnote.txt\nsub/"}}}
{"event":"step_update","step_update":{"step_index":5,"state":"ACTIVE","step_type":"agent_response","text_delta":"**Directory contents**…"}}
{"event":"result","result":{"status":"SUCCESS",…,"num_turns":1,"usage":{"input_tokens":25763,"output_tokens":559,…}}}
```

`step_type` values observed across all runs: `user_input, checkpoint, agent_response,
tool, system_message, error_message, unknown`. `result.status` values: `SUCCESS,
ERROR, CANCELED`. The >64 KiB sample cannot exist:

```
$ agy -p "run: python3 -c \"print('a'*70000)\"" $F
… "tool_name":"run_command",… "output":"<truncated 24 bytes>\naaaa…"   (AGY's own cap, ~8 KiB)
```

### 1.1.7 — sandbox / permission flags

```
$ agy --sandbox=bogus -p hi
invalid boolean value "bogus" for  -sandbox: strconv.ParseBool: parsing "bogus": invalid syntax
Usage of agy: …   exit 2
$ agy -p 'run: echo noflags > hello3.txt' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
{"event":"init","init":{"permission_mode":"request-review",…}}
{"event":"result","result":{"status":"CANCELED","response":"",…}}   exit 0
stderr: jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied. … re-run with --dangerously-skip-permissions to auto-approve all tools.
$ agy -p 'run: ls -la' $F           → init.permission_mode "always-proceed", tool runs, exit 0
```

Interactive: `pane-captures/1.1.7-interactive-startup.txt` (both flags accepted,
prompt renders) and `1.1.7-interactive-noflags-startup.txt`; without the skip flag the
native prompt of 1.1.14 appears. A hook `{"decision":"allow"}` does not override the
headless auto-deny (1.1.24 `x5b`).

### 1.1.8 — cancellation

Print mode (`cancel.sh`: start the turn in the background, wait for the tool step, signal):

```
$ agy -p 'run: sleep 40; echo finished-after-sleep' $F --print-timeout 3m --model gpt-oss-120b-medium &
# children of agy: python3.14 (gobby mcp-server), zsh → sleep 40
$ kill -INT $PID      # and, in a second run, kill -TERM $PID
{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"sleep 40; echo finished-after-sleep"}}}}
{"event":"result","result":{"status":"ERROR","response":"","error":"timeout waiting for response","duration_seconds":8.582711,"num_turns":1}}
--- sent SIGINT; exit code 1 after 0s        (SIGTERM: exit code 1 after 0s, duration_seconds 19.295677)
orphans after 2s:  20182 20181 S    sleep 40   (the shell child; the MCP child is gone)
--- after 45s: sleep still present?  (no sleep 40)   → the orphan ran to completion
```

The final record is byte-identical to timeout expiry (1.1.13); the tool step stays
`ACTIVE`; resume afterwards works (1.1.1). SIGINT before the first model call is
ignored and the turn completes.

Terminal (`pane-captures/1.1.8-interactive-ctrl-c.txt`, `1.1.8-interactive-exit.txt`):

```
> run: sleep 40; echo done-after-sleep
○ Bash(sleep 40; echo done-after-sleep) (ctrl+o to expand)
  ⎿  Interrupted · What should Antigravity CLI do instead?
  ● [05:48:19] sleep 40; echo done-after-sleep running
(second C-c at idle) press ctrl+c again to exit
(third C-c) session exits; ps: 7350  7349  00:21 sleep 40   → orphaned
```

Hook captures for that turn: `PreInvocation`, `PreToolUse`, `PostInvocation` — no
`PostToolUse`, no `Stop` on interrupt or on exit (`daemon-receipts.jsonl` turn 3,
`turn_ended_by_stop: false`). `esc` mid-turn interrupts the same way.

### 1.1.9 — network and state footprint

`net.sh`: a print-mode turn (`run: echo netprobe`) sampled with `lsof -nP -i` /
`lsof -nP` on the process tree every 0.3 s, then `find ~/.gemini -newer marker`.

```
remote hosts:  13.107.246.38, 150.171.109.183 (playwright{,-akamai,-verizon}.azureedge.net, browser-driver download attempt),
               142.250.100.132 / 172.217.113.4 / 172.217.114.4 (1e100.net = googleapis: oauth2.googleapis.com, accounts.google.com, play.googleapis.com),
               34.54.84.110 (googleusercontent.com)
cli log URL hosts:  8 daily-cloudcode-pa.googleapis.com      (model API)
written under <AGY_APP_DATA>/:  bin/agentapi (rewritten on every launch), brain/<CONVERSATION_ID>/.system_generated/logs/{transcript,transcript_full}.jsonl and chunks/…/00000000.jsonl,
               cache/last_conversations.json, cache/onboarding.json, conversations/<CONVERSATION_ID>.db(-wal,-shm), conversation_summaries.db,
               crashes/crash_<pid>_<uuid>.log, log/cli-<date>.log, mcp/gobby/<tool>.json + instructions.md, presence/<CONVERSATION_ID>.lock, knowledge/knowledge.lock
read:          ~/.gemini/config/projects/*.json, ~/Library/Caches/ms-playwright-go/, login Keychain (security list-keychains: ~/Library/Keychains/login.keychain-db)
```

### 1.1.10 — `RUN_COMMAND` transcript records

```
$ agy -p "run: ls -la" $F                          # zero exit
$ agy -p "run: sh -c 'echo boom >&2; exit 7'" $F   # nonzero exit
stream: {"step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","tool_info":{…,"output":"boom\n"}}   result SUCCESS, exit 0
PostToolUse payload: "error": ""
transcript_full.jsonl:
{"step_index":2,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","tool_calls":[{"name":"run_command","args":{"CommandLine":"sh -c 'echo boom >&2; exit 7'","Cwd":"<WORKSPACE>",…}}]}
{"step_index":3,"source":"MODEL","type":"GENERIC","status":"DONE","content":"Created At: …\nCompleted At: …\n\nThe command exited with code 7.\nOutput:\nboom\n\n"}
record census (1.1.18): USER_EXPLICIT/USER_INPUT 7, SYSTEM/CHECKPOINT 6, MODEL/PLANNER_RESPONSE 28, MODEL/GENERIC 20, SYSTEM/SYSTEM_MESSAGE 1 — no RUN_COMMAND, no exit_code field
```

Full records in `transcript-manifest.json` (`zero_exit_run_command`,
`nonzero_exit_run_command`); the 1.1.10-era `RUN_COMMAND` record is kept there for the
delta.

### 1.1.11 — outcome table

The table above; revisions applied to plan §2.2, §2.3, §2.5, §4.1, §4.2, §5.1, §5.2,
§5.3, §6.1, §6.2, §6.3, §6.4, §7.1 (plan §1.2 Run 2).

### 1.1.12 — controlled-tool bridge

Capture hook answering `{"decision":"deny","reason":"gate0: tool not in allowed set"}` on `PreToolUse`:

```
$ agy -p 'run: echo denied-probe' $F
{"event":"step_update","step_update":{"step_index":3,"state":"ERROR","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"echo denied-probe"},"error":{"type":"TOOL_ERROR","message":"tool call denied by pre-tool hook: gate0: tool not in allowed set"}}}}
{"event":"result","result":{"status":"ERROR","response":"The tool call to run the command was denied by the environment's pre-tool safety hook:…","error":"tool call denied by pre-tool hook: gate0: tool not in allowed set"}}   exit 0
(no PostToolUse capture for the denied step)
$ agy mcp list
NAME   TYPE   STATUS   COMMAND/URL
gobby  stdio  enabled  ~/Projects/gobby/.venv/bin/gobby mcp-server
MCP PreToolUse payload: "toolCall":{"name":"call_mcp_tool","args":{"ServerName":"gobby","ToolName":"list_mcp_servers","Arguments":{}}}
```

### 1.1.13 — `--print-timeout`

```
$ agy --print-timeout banana -p hi
invalid value "banana" for flag -print-timeout: time: invalid duration "banana"
Usage of agy: …   exit 2
$ agy --help | grep print-timeout   →  --print-timeout duration   … (default 5m0s)
$ agy --print-timeout 0 -p hi --output-format json
{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"timeout waiting for response",…}   exit 1   (no disable sentinel)
$ agy --print-timeout 2562047h -p 'reply with exactly: ok' --output-format json --sandbox=false --add-dir <WORKSPACE>
{"status":"SUCCESS","response":"ok\n",…}   exit 0   (effectively unbounded)
$ agy -p 'run: sleep 25; echo done' --print-timeout 8s --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE>
stderr: Error: timeout waiting for response   exit 1   (text mode: stdout empty)
$ agy -p 'run: sleep 25; echo done' --output-format stream-json --print-timeout 8s …
{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"run_command",…}}
{"event":"result","result":{"status":"ERROR","response":"","error":"timeout waiting for response","duration_seconds":7.023146}}   exit 1
```

Whole-turn clock; mid-stream expiry leaves the tool `ACTIVE` and its shell child
running; per turn under `--input-format stream-json` (1.1.18, `if-idle`).

### 1.1.14 — terminal plan menu and keystrokes

`pane-captures/1.1.14-interactive-*.txt`:

```
shift+tab        status line cycles: (default) → accept-edits → "Plan mode: research & plan only (shift+tab to cycle)"
> /plan create a file plan1.txt containing the word hi
● Create(~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/plan_create_plan1_txt.md) (ctrl+o to expand)
  Artifact: plan_create_plan1_txt.md
ctrl+r (or /artifact):
Action required (1 left)
› □ new plan_create_plan1_txt.md   open  approve reject
Keyboard: ↑/↓ Navigate  y/n Approve/reject  shift+a Approve all  p Preview  ctrl+g open in editor  esc Done
after y:   ⎿  Review submitted   then a user turn "> [Approved] plan_create_plan1_txt.md" (still in plan mode)
? overlay:  shift+tab  Cycle mode · ctrl+r  Review artifact · ctrl+o  Toggle trajectory view · esc Close
native permission prompt (session without --dangerously-skip-permissions):
Requesting permission for:
   echo permtest
Do you want to proceed?
> 1. Yes
  2. Yes, and always allow in this conversation for commands that start with 'echo'
  3. Yes, and always allow for commands that start with 'echo' (Persist to settings.json)
  4. No
  ↑/↓ Navigate · tab Amend · ctrl+g edit/expand command
esc to cancel
```

`Enter` selects the highlighted option; `1` ran the command
(`1.1.14-interactive-after-allow.txt`).

### 1.1.15 — authentication footprint

```
$ security find-generic-password -s gemini | awk '/acct|svce/'
    "acct"<blob>="antigravity"
    "svce"<blob>="gemini"
$ env -i HOME=~ PATH=/usr/bin:/bin:~/.local/bin ~/.local/bin/agy -p '/usage' --output-format json --print-timeout 1m
{"conversation_id":"","status":"SUCCESS",…"command":{"name":"usage",…}}   exit 0   (cli log: ChainedAuth: authenticated via keyring)
$ env GOOGLE_API_KEY=<REDACTED> GEMINI_API_KEY=<REDACTED> GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/creds.json ~/.local/bin/agy -p 'reply with exactly: ok' --output-format json --print-timeout 2m --model gpt-oss-120b-medium --add-dir <WORKSPACE>
{"status":"SUCCESS","response":"ok\n","num_turns":1}   exit 0   (ambient vars ignored)
$ env HOME=<PROBE_SCRATCH>/fakehome ~/.local/bin/agy -p '/usage' --output-format json --print-timeout 1m
stderr: https://accounts.google.com/o/oauth2/v2/auth?…&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&…
        Waiting for authentication (timeout 60s)...  Or, paste the authorization code here and press Enter:  Error: authentication timed out.
{"conversation_id":"","status":"ERROR","error":"authentication failed or timed out",…}   exit 1
```

Credential = the login Keychain item, gated on state under the real `<AGY_APP_DATA>/`;
no credential env var is accepted; no auth-CLI inference is needed.

### 1.1.16 — compaction signaling

All stream-json captures (including the 87 s, 301 k-token `x7-forcecont` turn with 46
model invocations and the 10-continuation `x10-stop-continue` turn) were scanned for
`step_type` / `event` values: only `user_input, checkpoint, agent_response, tool,
system_message, error_message, unknown` and `init, step_update, result` occur. The
sole adjacent signal:

```
{"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"checkpoint","duration_seconds":0.954066}}   (step 1–2 of every conversation, even single-turn)
transcript: {"step_index":1,"source":"SYSTEM","type":"CHECKPOINT",…}
```

### 1.1.17 — interactive dispatch

Receipt run, one tmux session (`pane-captures/1.1.17-interactive.txt`,
`1.1.5-interactive-*.txt`):

```
$ tmux new-session -d -s agy-gate0 -x 200 -y 50 -c <WORKSPACE> "agy --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium --add-dir <WORKSPACE>"
$ tmux send-keys -t agy-gate0 -l 'list the files in this directory'; tmux send-keys -t agy-gate0 Enter      # then 'run: ls -la', then the MCP prompt
$ tmux capture-pane -p -S -200 -t agy-gate0
```

`daemon-receipts.jsonl` interactive lines: turn 0 (`list_dir`), turn 1
(`run_command`), turn 2 (`view_file` + `call_mcp_tool`), turn 3 (interrupted
`run_command`). Payload key sets per event are identical to print mode
(`hook-payloads.jsonl`: every interactive line's `payload` keys equal the print line's
for the same event). Negatives in both modes: no `PostToolUse` for a `TOOL_ERROR`
step, no `Stop` on interrupt/exit.

### 1.1.18 — `--input-format stream-json`

`inputfmt.py` drove `agy --input-format stream-json --output-format stream-json
--sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE> --print-timeout 2m
--model gpt-oss-120b-medium` over a pipe:

```
>> {"event": "user", "message": {"content": "reply with exactly: one"}}
[2.4] init {"model":"gpt-oss-120b-medium","cwd":"<WORKSPACE>","permission_mode":"always-proceed"}
[4.0] result {"status":"SUCCESS","num_turns":1}
>> {"event": "user", "message": {"content": "what did you reply last time? one word"}}
[47.6] result {"status":"SUCCESS","num_turns":2}            (same conversation_id)
[47.7] closing stdin (EOF) → exit code 0, no extra record
--conversation <CONVERSATION_ID>: accepted; first result num_turns 3, next 4
--print-timeout 20s, 25 s idle between turns: alive after idle: True; second turn result SUCCESS → per-turn clock
>> {"event": "user", "message": {"content": "run: sleep 30; echo slept"}}  … SIGINT to agy pid
[21.2] result {"status":"ERROR","error":"context canceled","num_turns":1}   alive after SIGINT: False rc=1
shapes: {"event":"bogus_event"} → stderr `warning: ignoring unsupported stream input message event "bogus_event"`, ignored
        {"event":"user","message":{"content":[{"type":"text","text":"…"}]}} → accepted
        raw text line → result ERROR "failed to decode stream input: invalid character 'r' looking for beginning of value", exit 1
        {"message":{…}} (no event) → result ERROR "stream input message is missing the \"event\" field", exit 1
agy --input-format stream-json … -p   → stderr "flag needs an argument: -p" + usage, exit 2 (launch without -p; the prompt comes over stdin)
```

### 1.1.19 — usage / quota / credits

```
$ agy -p "/usage" --output-format json
{"conversation_id":"","status":"SUCCESS","response":"Gemini Models\tWeekly Limit Remaining\t0%\t2026-08-27T08:53:33Z\nClaude and GPT models\tWeekly Limit Remaining\t98%\t2026-08-29T08:43:38Z\n","duration_seconds":0,"num_turns":0,"usage":{…all 0…},
 "command":{"name":"usage","data":{"description":"Within each group, models share a weekly limit. …","groups":[{"name":"Gemini Models","description":"Models within this group: Gemini Flash, Gemini Pro","buckets":[{"id":"gemini-weekly","name":"Weekly Limit Remaining","description":"You have hit your weekly limit, it refreshes in 4 days, 22 hours. …","window":"weekly","remaining_fraction":0,"reset_time":"2026-08-27T08:53:33Z"}]},{"name":"Claude and GPT models",…"buckets":[{"id":"3p-weekly",…"remaining_fraction":0.9783130288124084,"reset_time":"2026-08-29T08:43:38Z"}]}]}}}   exit 0
$ agy -p "/quota" --output-format json      → identical body, "command":{"name":"usage",…}   exit 0
$ agy -p "/credits" --output-format json
{"conversation_id":"","status":"ERROR","response":"","error":"/credits failed: retrieving credits: no credits info found",…}   exit 1
$ agy -p 'reply with exactly: ok' --output-format json --print-timeout 1m       # Gemini default model, bucket at 0
{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 120h9m43s.","duration_seconds":0,"num_turns":1,…}   exit 1
```

`num_turns 0` and zero usage on `/usage` `/quota`: no agent turn, no spend.

### 1.1.20 — model list

```
$ agy models --output-format json
Usage: agy models [flags]
…
Error: flags provided but not defined: -output-format   exit 1   (same for stream-json)
$ agy --output-format json models
{"conversation_id":"","status":"SUCCESS","response":"gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n…","num_turns":0,…,"command":{"name":"models","data":{"models":[{"id":"gemini-3.7-flash-high","label":"Gemini 3.7 Flash (High)"},…,{"id":"gpt-oss-120b-medium","label":"GPT-OSS 120B (Medium)"}]}}}   exit 0
$ agy --output-format stream-json models    → {"event":"command_result",…} then {"event":"result",…}   exit 0
$ agy -p "/model" --output-format json
{…"command":{"name":"model","data":{"id":"gemini-3.5-flash-high","label":"Gemini 3.5 Flash (High)","effort":"high","is_default":false}}}   exit 0
$ agy --effort bogus -p hi
Error: invalid model selection (--model "" --effort "bogus"): invalid --effort "bogus" (valid: low, medium, high)   exit 1   (effort is the id suffix)
$ H=$(mktemp -d); HOME="$H" agy --output-format json models < /dev/null; echo "rc=$?"; ls -R "$H"
Fetching available models...
Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
rc=1
Library/Caches/ms-playwright-go/1.57.0   (isolated HOME; stdout empty; no OAuth prompt; real Keychain and ~/.gemini untouched)
$ HOME="$H" agy --output-format stream-json models < /dev/null   → same stderr, rc=1;   HOME="$H" agy models → same, rc=1
```

No default marker and no effort field on the list; the default lives in `/model`.

### 1.1.21 — `/hooks` introspection

```
$ agy -p "/hooks" --output-format json       # before install
{"conversation_id":"","status":"SUCCESS","response":"gobby\tenabled\t…","num_turns":0,…,"command":{"name":"hooks","data":{"hooks":[{"name":"gobby","enabled":true,"source":"~/.gemini/config/hooks.json","actions":[{"event":"PreInvocation","type":"command","command":"~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PreInvocation","timeout_seconds":45},{"event":"PreToolUse","matcher":"*",…},…5 actions]}]}}}   exit 0
# with gate0-capture installed: hooks[] = [{"name":"gate0-capture","enabled":true,…5 actions},{"name":"gobby",…}]
# {"enabled": false} hook: appears with "enabled":false and its actions
# malformed {"Stop":[{"type":"command"}],"NotAnEvent":[]}: appears "enabled":true with an action lacking "command"; "NotAnEvent" vanishes; no warning
# after removal (receipt run, command-captures.json "hooks-after"): hooks[] = [{"name":"gobby",…5 actions}]; hooks.json == pre-probe copy
# unauthenticated (token refresh blocked by the sandbox proxy): OAuth prompt on stderr, exit 1 "authentication failed or timed out"
```

### 1.1.22 — transcript layout

`transcript-manifest.json` (`layout`, `record_keys_1_1_18`, `truncation_sample`):

```
brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl   append-only, complete, native-typed tool_calls[].args — the file transcriptPath names; THE PARSER INPUT
brain/<CONVERSATION_ID>/.system_generated/logs/transcript.jsonl        token-efficient twin: content ≤ ~4 KiB + "truncated_fields":["content"], args JSON-string-encoded
brain/<CONVERSATION_ID>/.system_generated/logs/chunks/{transcript,transcript_full}/00000000.jsonl   byte-identical copies (cmp → identical in every conversation; largest 10,751 bytes; a second chunk never opened)
```

### 1.1.23 — `--mode plan|accept-edits`

```
$ agy -p 'create a file hello.txt containing the word hi' --mode plan --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
{"event":"init","init":{"expanded_commands":[{"name":"plan","type":"system"}],"permission_mode":"request-review",…}}
… "tool_name":"write_to_file","tool_info":{"name":"write_to_file","parameters":{"TargetFile":"~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/create_hello_txt.md"}} …
{"event":"result","result":{"status":"SUCCESS","response":"… I have created the implementation plan at [create_hello_txt.md](file://~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/create_hello_txt.md)…"}}   exit 0
$ ls <WORKSPACE>/hello.txt → No such file   (no workspace write, no approval record)
$ agy -p 'create a file hello2.txt containing the word hi' --mode accept-edits --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
… "tool_name":"write_to_file",…"TargetFile":"<WORKSPACE>/hello2.txt" … {"event":"result","result":{"status":"SUCCESS","response":"I have created [hello2.txt](file://<WORKSPACE>/hello2.txt) containing the word `hi`.\n"}}   exit 0   (init.permission_mode stays "request-review")
$ agy --mode bogus -p hi
stderr: warning: unrecognized --mode value "bogus" (valid: accept-edits, plan)   → runs in default mode, exit 0
```

Terminal: `pane-captures/1.1.23-interactive-plan-mode.txt`,
`1.1.23-interactive-artifact-review.txt` (menu and keystrokes as in 1.1.14).

### 1.1.24 — response-field live acceptance

Each probe set the capture hook's answer for one event (`hook-payloads.jsonl` record
`1.1.24`, field `outcome`), then ran a print-mode turn:

```
PreToolUse {"decision":"deny","reason":"…"}                       → see 1.1.12: tool ERROR, result ERROR, exit 0                                  honored
PreToolUse {"decision":"deny_unless_prior_grant","reason":"gate0 dupg"}
  $ agy -p 'run: echo dupg-probe' $F                              → tool DONE "output":"dupg-probe\n", result SUCCESS (grant = --dangerously-skip-permissions)   honored
  $ agy -p 'run: echo dupg-probe2' … (no skip flag)                → tool ERROR "Permission denied for command(echo dupg-probe2). gate0 dupg", result ERROR   honored
PreToolUse {"decision":"allow","overwrite":{"CommandLine":"echo overwritten-by-hook"}}
  $ agy -p 'run: echo original-command' $F                        → stream parameters still "echo original-command", "output":"overwritten-by-hook\n"          honored
PreToolUse {"decision":"allow","permissionOverrides":["command(echo permov-probe)"]} (no skip flag)
  $ agy -p 'run: echo permov-probe' …                             → tool ERROR "user denied permission to run command", result ERROR, exit 1                  NOT honored
  $ agy -p 'run: echo permov-probe-b' … ({"decision":"allow"})    → result CANCELED, exit 0, stderr "jetski: no output produced … auto-denied"                  NOT honored
PostInvocation {"terminationBehavior":"terminate"}
  $ agy -p 'run: echo step-one, then run: echo step-two, …' $F    → only step-one ran; result SUCCESS "response":""; Stop.terminationReason "TERMINAL_CUSTOM_HOOK"   honored
PostInvocation {"terminationBehavior":"force_continue"}
  $ agy -p 'reply with exactly: ok' $F --print-timeout 90s        → 46 PreInvocation/PostInvocation pairs until expiry, exit 1                               honored
PreInvocation {"injectSteps":[{"toolCall":{"name":"run_command","args":{"CommandLine":"echo injected-tool-ran"}}}]}
  $ agy -p 'reply with exactly: ok' $F                            → step 1 "error_message"; result ERROR "Agent execution terminated due to error."; cli log "unknown injected step type: <nil>"; Stop.terminationReason "ERROR"; exit 1   NOT honored
PreInvocation {"injectSteps":[{"userMessage":"…PINEAPPLE…"},{"ephemeralMessage":"…"}]}
  $ agy -p 'reply with exactly: ok' $F                            → result SUCCESS "response":"OK PINEAPPLE\n", num_turns 2                                   honored
Stop {"decision":"continue","reason":"gate0: keep going, say DONE-N with N incremented each time"}
  $ agy -p 'reply with exactly: DONE-1' $F                        → "response":"DONE-1\nDONE-2\n…DONE-11\n": 10 continuations honored, the 11th ignored (forced end); Stop.terminationReason NO_TOOL_CALL   honored ×10
PreToolUse {"decision":"allow"} + exit 1 (stderr "gate0 hook stderr message exit1")
  $ agy -p 'run: echo exit1-probe' $F                             → every tool: "JSON hook \"jsonhook__gate0-capture_PreToolUse_0_0\" failed: command failed: exit status 1, stderr: gate0 hook stderr message exit1"; result ERROR; exit 0   fail-closed, stdout ignored
PreToolUse {"decision":"allow"} + exit 2                            → same with "exit status 2"                                                                  fail-closed
Stop {} + exit 2
  $ agy -p 'reply with exactly: ok' $F                            → result SUCCESS "ok\n"; Stop hook exit ignored                                             ignored
```

`Stop.decision` enum: `stop|continue|block`.

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
- `ghook` posts to the daemon only for a managed context (`GOBBY_PROJECT_ID` /
  `GOBBY_SESSION_ID` / `GOBBY_AGENT_RUN_ID`, a `.gobby/project.json` under a
  `workspacePaths` entry, or `project_id` in the payload); an unmanaged AGY launch
  gets the skip JSON and no daemon receipt.

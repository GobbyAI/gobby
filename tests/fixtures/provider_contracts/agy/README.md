# AGY Provider Contract Captures (Gate 0, plan `agy-full-integration` §1.1)

Captured 2026-08-22 by task #19563 in **both** print mode (`agy -p … --output-format
stream-json`) and interactive terminal mode (raw tmux, plan §1.1 mechanics). Three runs:
the full both-mode probe (03:17–04:30 local), the **daemon-receipt run** (05:40–05:50
local) that re-ran the three fixed prompts per mode with the real `ghook` delivering
every hook event to the daemon, and the **pass-3 interactive run** (06:36–07:20 local,
11:36–12:20Z) that answered every live-turn record a second time in a tmux pane —
including all 1.1.24 response-field variants with `gate0-capture` returning the
configured responses.

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
later turns — including the whole receipt run and the pass-3 run — used
`--model gpt-oss-120b-medium` (separate bucket); records note the model where it
matters.

## Files

| File | Records | Content |
| --- | --- | --- |
| `hook-payloads.jsonl` | 1.1.1, 1.1.3, 1.1.4, 1.1.5, 1.1.9, 1.1.10, 1.1.13, 1.1.17, 1.1.24 | 284 live camelCase payloads with `mode` (`print` / `interactive`), hook cwd, env, the capture hook's answer and exit code; the 49 receipt-run lines carry `envelope_id`, which joins them to `daemon-receipts.jsonl`; the 208 `"pass": 3` lines are the 150 pass-3 interactive/print turns (every 1.1.24 interactive variant carries the configured `response`) plus 58 first-run print-mode 1.1.24 lines (`force_continue` ×47, `Stop continue` ×10, `injectSteps` messages ×1) added so both modes carry every variant |
| `daemon-receipts.jsonl` | 1.1.5, 1.1.17 | 49 daemon-side receipts, one per `ghook` delivery of the receipt run: `mode`, `event`, `tool_class` (`built-in` / `shell` / `mcp`), the daemon's HTTP status + response body, the daemon's processed-envelope marker (`~/.gobby/hooks/inbox/processed/<sha256(envelope_id)>.json`), and the matching `~/.gobby/logs/hooks.log` line |
| `pane-captures/<record>-interactive[-<label>].txt` | every `both` record below | scrubbed `tmux capture-pane -p -S -200` output, one file per cited terminal-mode observation; three header lines then the pane body |
| `evidence/<record>-<mode>-<label>.txt` | every record below | the literal command(s) (`$ …`) and the complete scrubbed stdout / stderr / exit code (or the probe script's report) the record's verdict rests on; lines over 4 KiB carry `<TRUNCATED n bytes>` |
| `transcript-manifest.json` | 1.1.2, 1.1.10, 1.1.22 | transcript layout, literal `transcriptPath`, record census (print and interactive), zero/nonzero-exit shell records, truncation evidence |
| `stream-json-samples.jsonl` | 1.1.1, 1.1.6, 1.1.8, 1.1.13, 1.1.18, 1.1.20 | scrubbed NDJSON records (init, resumed turn, text_delta, tool ACTIVE/DONE/ERROR, failure results, stream-input errors, synthetic malformed line) |
| `command-captures.json` | 1.1.7, 1.1.13, 1.1.15, 1.1.19, 1.1.20, 1.1.21 | `/hooks` before/with/after the capture hook (all three runs), `/usage` `/quota` `/credits` `/model`, `models`/`agents` JSON, isolated-HOME `models`, `mcp list`, flag-syntax errors, auth probes |

Deleted: `agy_models_v1.0.10.txt`, `model-cache-summary.json` (superseded by
`command-captures.json`).

## Capture procedure

1. `agy -p "/hooks" --output-format json` (before). Install `gate0-capture` beside
   `gobby` in `~/.gemini/config/hooks.json`: five events, `timeout` 45, command
   `gate0-capture.sh <Event>` writing stdin verbatim plus `PWD` and
   `ANTIGRAVITY_CONVERSATION_ID` to `<scratch>/hook-captures/NNNN-<mode>-<event>.json`
   and answering `{"decision":"allow"}` (PreToolUse) / `{}` (others), with per-event
   response / exit-code / stderr override files for 1.1.24 (`.response-<Event>`,
   `.exit-<Event>`, `.stderr-<Event>`, optional `.budget-<Event>` use count). `/hooks`
   again (both hooks listed).
2. Print-mode turns in a throwaway workspace: built-in (`list the files in this
   directory`), shell (`run: ls -la`), MCP (`call the gobby list_mcp_servers tool and
   report the result`), plus the targeted probes per record below. 566 hook
   invocations captured in the first run, 150 in the pass-3 run.
3. Interactive: `tmux new-session -d -s agy-gate0 -x 200 -y 50 -c <WORKSPACE> "agy
   --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium
   --add-dir <WORKSPACE>"`; prompt glyph `>` on its own line between two horizontal
   rules, status line `? for shortcuts … <model label>`; prompts sent with `tmux
   send-keys -t agy-gate0 -l '<prompt>'; tmux send-keys -t agy-gate0 Enter`, the pane
   captured after the capture hook's `Stop` file appeared (or, for multi-Stop turns,
   after 30 s without a new hook file while the idle status line showed). Second
   sessions without the two flags (native permission prompt, 1.1.24 no-skip variants),
   with `--print-timeout 8s` (1.1.13), with `--conversation <id>` (1.1.1), and under
   `HOME=$(mktemp -d)` (1.1.15). `tmux set-option remain-on-exit on` keeps the dead
   pane readable for the exit captures. `tmux kill-session` and orphan check at the end.
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
   is byte-equivalent to the pre-probe backup (`cmp` exit 0, all three runs;
   `evidence/1.1.21-print-hooks.txt`).
6. Scrub: `$HOME`→`~`, conversation ids→`<CONVERSATION_ID>`, workspace→`<WORKSPACE>`,
   probe scratch→`<PROBE_SCRATCH>`, isolated HOME→`<ISOLATED_HOME>`, app-data paths
   other than `brain/<id>/…`→`<AGY_APP_DATA>/…`, `~/.gemini/config/projects/<uuid>.json`
   →`<PROJECT_UUID>`, the local user name→`<USER>`, project id→`<PROJECT_ID>`, AGY
   error ids→`<ERROR_ID>`, emails/tokens/OAuth `client_id`/`code_challenge`/`state`→
   `<REDACTED>`, `ps` lines cut at 160 columns→`<TRUNCATED_PATH>`, tool output >4 KiB
   truncated with `<TRUNCATED n bytes>`. `ghook` envelope ids (`n-<ms>-<uuid>`) are
   kept: they key the daemon markers and are not conversation ids.

## Contract-outcome table (1.1.11)

Outcomes: **confirmed** (open record answered as the plan assumed), **re-confirmed
unchanged** (1.1.10 record identical on 1.1.18), **disproven** (observed behaviour
contradicts plan text; the affected sections were revised), **negative** (the
capability is absent; recorded as a negative contract). **Modes**: `both` — the record
involves a live agent turn and is answered in print mode and in an interactive pane;
`print` — the record is a print-mode-only surface (stdin transport); `command` — no
agent turn runs (slash command, subcommand, or this table). Every row's literal
command(s) and observed output are in "Record evidence" below and in the cited
`evidence/` file.

| Record | Modes | Outcome | Summary |
| --- | --- | --- | --- |
| 1.1.1 resume | both | **re-confirmed unchanged** (+1 delta) | `--conversation <id>` resumes in both modes: same id, `num_turns` 1→2, prior turn recalled (and rendered in the pane), also after SIGINT/SIGTERM. Delta: `duration_seconds` is cumulative per conversation. |
| 1.1.2 transcriptPath | both | **re-confirmed, layout disproven** | Literal value `~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl` in both modes; no workspace-local file. |
| 1.1.3 cwd remedy | both | **confirmed, remedy named** | Unregistered cwd → `workspacePaths []`, tools target app data. Remedy: `--add-dir <cwd>` on every launch. |
| 1.1.4 image input | both | **negative** | No image flag; `@path` is plain text; stream-input image blocks rejected; only the model's own `view_file` sees an image (Gemini); gpt-oss interactively declines. |
| 1.1.5 payloads | both | **confirmed (hook→daemon receipts in both modes)** | Five camelCase events per mode for a built-in, a shell and an MCP tool; 49 daemon receipts (HTTP 200 + marker + `hooks.log` line). `source=agy` line / session row are §4.1's acceptance. |
| 1.1.6 stream-json | both | **re-confirmed unchanged** (+2 deltas) | Nested `{"event":…,"<event>":{…}}`; 57 tools; `CANCELED` status; no >64 KiB sample (AGY caps at ~8 KiB). |
| 1.1.7 sandbox flags | both | **re-confirmed unchanged** | `--sandbox` boolean, `--sandbox=false` accepted both modes; skip flag → `always-proceed`; without it headless tools auto-deny, interactive prompts. |
| 1.1.8 cancellation | both | **confirmed** | SIGINT/SIGTERM exit 1 with the timeout payload, shell child orphaned, resume works; terminal `C-c` interrupts without `Stop`; second idle `C-c` exits status 0 with a resume hint, no `Stop`. |
| 1.1.9 network/roots | both | **confirmed** | Google API/OAuth/telemetry hosts plus Playwright CDN; app-data roots enumerated below; interactive adds `history.jsonl`. |
| 1.1.10 RUN_COMMAND | both | **disproven** | Both exit classes are `MODEL/GENERIC` free text in both modes; no `RUN_COMMAND` record, no structured `exit_code`. |
| 1.1.11 outcome table | command | **confirmed** | This table. |
| 1.1.12 controlled-tool bridge | both | **confirmed (supported)** | `PreToolUse` `decision:"deny"` transport in both modes; MCP tools surface as `call_mcp_tool`. |
| 1.1.13 `--print-timeout` | both | **re-confirmed unchanged** (+2 deltas) | Go syntax, default `5m0s`, no disable sentinel, expiry exit 1; under `json|stream-json` a stdout `result{status:ERROR}`; inert in interactive mode. |
| 1.1.14 terminal plan menu | both | **confirmed** | `shift+tab` cycles modes; `ctrl+r`/`/artifact` review with `y`/`n`/`shift+a`/`p`/`esc`; permission prompt `1`–`4`/`esc`; headless `--mode plan` is the print half (1.1.23). |
| 1.1.15 auth footprint | both | **confirmed** | Keychain item `svce=gemini acct=antigravity`; env API-key vars ignored; isolated `HOME` → print: `Please sign in` exit 1, interactive: login-method menu. |
| 1.1.16 compaction | both | **negative** | No compaction/context-pressure record in any stream or transcript; `checkpoint` fires at step 1 of every conversation. |
| 1.1.17 interactive dispatch | both | **confirmed** | All five events fire interactively with key sets identical to print mode; negatives apply to both modes. |
| 1.1.18 `--input-format stream-json` | print | **confirmed** | One `result` per turn; EOF → exit 0; per-turn timeout; `--conversation` accepted; SIGINT kills the process (`context canceled`, exit 1). |
| 1.1.19 usage/quota | command | **confirmed; `/credits` negative** | `/usage` shape recorded; `/quota` aliases it; `/credits` exit 1; exhausted = `remaining_fraction 0` + turn `result ERROR`. |
| 1.1.20 models | command | **disproven (placement), shape confirmed** | `agy models --output-format json` exit 1; `agy --output-format json models` → `models[].{id,label}`; unauthenticated (isolated HOME) exit 1, empty stdout, no prompt. |
| 1.1.21 `/hooks` | command | **confirmed** | `hooks[].{name,enabled,source,actions[]}`; disabled → `enabled:false`; malformed shows without warning; no agent turn. |
| 1.1.22 transcript layout | both | **confirmed** | Parser input `transcript_full.jsonl`; `transcript.jsonl` truncated twin; `chunks/` byte-identical copies in both modes; interactive adds `SYSTEM_SDK/*` and `SYSTEM/ERROR_MESSAGE` records. |
| 1.1.23 `--mode` | both | **confirmed** | Headless `plan` writes `brain/<id>/<name>.md`, no approval record; `accept-edits` writes without prompting; `bogus` → warning; terminal menu as 1.1.14. |
| 1.1.24 response fields | both | **confirmed with negatives** | Both modes: honored `deny`, `deny_unless_prior_grant`, `overwrite`, `terminationBehavior`, `injectSteps.userMessage`/`ephemeralMessage`, `Stop continue` ×10 then forced end. Not honored: `permissionOverrides` (headless auto-deny / interactive prompt still shown), `injectSteps.toolCall` (fatal). PreToolUse exit 1/2 blocks the tool; Stop exit 2 ignored. |

## Record evidence

Print-mode commands ran in `<WORKSPACE>` through the outside pane; `$F` below stands
for the common flag set `--output-format stream-json --sandbox=false
--dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE>`; the
`evidence/` file spells every command out in full. Quoted lines are complete lines
of the cited file. Interactive evidence is the cited `pane-captures/` file; the
pass-3 session prompts were sent with `tmux send-keys -t agy-gate0 -l '<prompt>'` +
`Enter`.

### 1.1.1 — resume (`--conversation`)

Print (`evidence/1.1.1-print-resume.txt`, `-resume-after-cancel.txt`, `-resume-timed.txt`):

```
$ agy -p 'what shell command did you run earlier in this conversation? answer in one line' --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE> --conversation <CONVERSATION_ID>
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"I ran `ls -la` earlier in this conversation.\n","duration_seconds":291.375441,"num_turns":2,"usage":{"input_tokens":36795,"output_tokens":816,"thinking_tokens":508,"cache_read_tokens":12194,"total_tokens":37611}}}
--- exit 0 ---
$ agy -p 'what command were you running before? one line' --output-format json --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE> --print-timeout 2m --model gpt-oss-120b-medium --conversation <CONVERSATION_ID>
{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The command I ran was:\n\n```bash\nsleep 40; echo finished-after-sleep\n```\n","duration_seconds":144.825136,"num_turns":2,"usage":{"input_tokens":26000,"output_tokens":415,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":26415}}
--- exit 0 ---
$ date +%s; agy -p 'reply with exactly: ok2' --output-format json --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE> --print-timeout 3m --model gpt-oss-120b-medium --conversation <CONVERSATION_ID>; date +%s
1787388425
{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"ok2\n","duration_seconds":213.192009,"num_turns":2,"usage":{"input_tokens":25696,"output_tokens":92,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":25788}}
1787388430
--- exit 0 ---
```

Same `conversation_id` as the `run: ls -la` turn, `num_turns` 1→2; the second command
resumes the SIGINT-cancelled conversation of 1.1.8. Delta: 5 s of wall clock reported
as `duration_seconds` 213 — measured from conversation creation.

Interactive (`pane-captures/1.1.1-interactive-resume.txt`): `tmux new-session -d -s
agy-gate0 … "agy --sandbox=false --dangerously-skip-permissions --model
gpt-oss-120b-medium --add-dir <WORKSPACE> --conversation <CONVERSATION_ID>"` on the
print-mode `run: ls -la` conversation (`evidence/1.1.10-print-zero-exit.txt`). The pane
renders the earlier print turn (`● Bash(ls -la)` and its listing) before the prompt;
the question `what shell command did you run earlier in this conversation? answer in
one line` is answered `ls -la`. The hook capture for that turn
(`hook-payloads.jsonl`, record `1.1.1 interactive resume (--conversation) turn`)
carries the same `conversationId` as the print lines (record `1.1.1 print turn`).

### 1.1.2 — literal `transcriptPath`

The value is read from the hook payload, so the command is the turn plus a field
extraction over the committed capture file:

```
$ agy -p 'run: ls -la' --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE> --model gpt-oss-120b-medium
$ python3 -c "import json; [print(r['mode'], r['payload']['transcriptPath'], r['payload']['artifactDirectoryPath']) for r in map(json.loads, open('hook-payloads.jsonl')) if r['event']=='PreInvocation' and r['payload']['invocationNum']==0 and r['record'].startswith('1.1.1 ')]"
print ~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl ~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>
interactive ~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl ~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>
$ find <WORKSPACE> -name 'transcript*'
(no output)
```

Every `PreInvocation` line in `hook-payloads.jsonl` (both modes, all three runs)
carries that literal; `transcript-manifest.json` `transcript_path_literal` repeats it.
`find` after every turn printed nothing: no workspace-local transcript is ever
created. Interactive pane for the same conversation:
`pane-captures/1.1.1-interactive-resume.txt`.

### 1.1.3 — cwd remedy

Unregistered cwd, no `--add-dir` (`evidence/1.1.3-print-unregistered-cwd.txt`,
`hook-payloads.jsonl` record `1.1.3 unregistered-cwd`):

```
$ agy -p "list the files in this directory" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"list_dir","tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"~/.gemini/antigravity-cli"}}}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ERROR","step_type":"tool","tool_name":"list_dir","duration_seconds":0.146195,"tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"~/.gemini/antigravity-cli"},"error":{"type":"TOOL_ERROR","message":"permission check failed for read_file \"~/.gemini/antigravity-cli\": Permission denied for read_file(~/.gemini/antigravity-cli). Matches hardcoded system protection boundary rule."}}}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":5,"state":"DONE","step_type":"tool","tool_name":"list_dir","duration_seconds":0.147008,"tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"<AGY_APP_DATA>/scratch"},"output":".antigravitycli/"}}}
--- exit 0 ---
PreToolUse payload line: "workspacePaths": []   (the run_command args carry no "Cwd")
```

Remedy and alternatives (`evidence/1.1.3-print-add-dir.txt`, `-project.txt`, `-new-project.txt`):

```
$ agy -p 'run: pwd' --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE>
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.21427,"tool_info":{"name":"run_command","parameters":{"CommandLine":"pwd"},"output":"<WORKSPACE>\n"}}}
--- exit 0 ---
$ agy -p 'run: pwd' --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --project <PROJECT_UUID>
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.21332,"tool_info":{"name":"run_command","parameters":{"CommandLine":"pwd"},"output":"<WORKSPACE>\n"}}}
--- exit 0 ---
$ agy -p "run: ls -la" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --new-project
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The `ls -la` command has completed successfully. Here is the output:\n\n```text\ntotal 8\ndrwxr-xr-x@  4 <USER>  staff  128 Aug 22 03:19 .\ndrwx------@ 13 <USER>  staff  416 Aug 22 03:21 ..\n-rw-r--r--@  1 <USER>  staff   12 Aug 22 03:19 note.txt\ndrwxr-xr-x@  3 <USER>  staff   96 Aug 22 03:19 sub\n```\n","duration_seconds":6.853879,"num_turns":1,"usage":{"input_tokens":19938,"output_tokens":763,"thinking_tokens":467,"cache_read_tokens":12194,"total_tokens":20701}}}
--- exit 0 ---
```

With `--add-dir` the PreToolUse payload reads `"workspacePaths": ["<WORKSPACE>"]` and
`"toolCall":{"args":{"CommandLine":"pwd","Cwd":"<WORKSPACE>"`. `--project` needs the
id from `~/.gemini/config/projects/<uuid>.json`; `--new-project` writes a new such file
per launch. Interactive: `pane-captures/1.1.3-interactive.txt` — `ListDir(<WORKSPACE>)`
under `--add-dir <WORKSPACE>`.

### 1.1.4 — image input

Print (`evidence/1.1.4-print-image.txt`, `-image-at.txt`, `1.1.18-print-input-format.txt`; Gemini default model):

```
$ agy --help | grep -i image
(no output)
$ agy -p 'describe the image file img.png in this directory: what color is it and what size?' --output-format stream-json --print-timeout 4m --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE>
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":5,"state":"DONE","step_type":"tool","tool_name":"view_file","duration_seconds":0.186393,"tool_info":{"name":"view_file","parameters":{"AbsolutePath":"<WORKSPACE>/img.png"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The image file [img.png](file://<WORKSPACE>/img.png) has the following attributes:\n\n* **Color**: Solid red\n* **Dimensions**: 64 x 64 pixels\n* **File Size**: 168 bytes\n","duration_seconds":11.027618,"num_turns":1,"usage":{"input_tokens":29622,"output_tokens":1955,"thinking_tokens":1192,"cache_read_tokens":56885,"total_tokens":31577}}}
--- exit 0 ---
$ agy -p '@img.png what color is this image?' --output-format stream-json --print-timeout 4m --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE>
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"find_by_name","tool_info":{"name":"find_by_name","parameters":{"Pattern":"*img.png*","SearchDirectory":"<WORKSPACE>"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The image [img.png](file://<WORKSPACE>/img.png) is **pure red** (RGB: `[255, 0, 0]`, Hex: `#FF0000`).\n","duration_seconds":12.483631,"num_turns":1,"usage":{"input_tokens":43272,"output_tokens":3251,"thinking_tokens":2145,"cache_read_tokens":44696,"total_tokens":46523}}}
--- exit 0 ---
$ python3 inputfmt.py shapes4      # stdin line: {"event": "user", "message": {"content": [{"type": "image", "source": "x"}]}}
[  67.1] result {"conversation_id": "<CONVERSATION_ID>", "status": "ERROR", "error": "stream input content block type \"image\" is not supported (only \"text\")", "duration_seconds": 6.628119, "num_turns": 2, "usage": {"input_tokens": 25585, "output_tokens"
[  67.4] exit code 1
```

`@path` is plain text (the model searched for it with `find_by_name`); the only
image path is the model's own `view_file`, so `VISION_EXTRACT` stays unavailable as a
Gobby binding. Interactive (`pane-captures/1.1.4-interactive-image.txt`, gpt-oss): the
same prompt produced only `● ListDir(<WORKSPACE>)` and the reply `I don’t have a way
to directly render or analyze the raw contents of img.png from the filesystem.` —
no attachment surface exists in either mode.

### 1.1.5 — live camelCase payloads and daemon receipts

Receipt run, print mode (three conversations, `hook-payloads.jsonl` lines with
`envelope_id`, `daemon-receipts.jsonl`):

```
$ agy -p 'list the files in this directory' $F --model gpt-oss-120b-medium
$ agy -p 'run: ls -la' $F --model gpt-oss-120b-medium
$ agy -p 'call the gobby list_mcp_servers tool and report the result' $F --model gpt-oss-120b-medium
$ python3 -c "import json; [print(r['mode'], r['event'], sorted(r['payload'])) for r in map(json.loads, open('hook-payloads.jsonl')) if r['record']=='1.1.5 daemon-receipt run (shell, turn 0)']"
print PreInvocation ['artifactDirectoryPath', 'conversationId', 'initialNumSteps', 'invocationNum', 'modelName', 'transcriptPath', 'workspacePaths']
print PreToolUse ['artifactDirectoryPath', 'conversationId', 'modelName', 'stepIdx', 'toolCall', 'transcriptPath', 'workspacePaths']
print PostToolUse ['artifactDirectoryPath', 'conversationId', 'error', 'modelName', 'stepIdx', 'toolCall', 'transcriptPath', 'workspacePaths']
print PostInvocation ['artifactDirectoryPath', 'conversationId', 'initialNumSteps', 'invocationNum', 'modelName', 'transcriptPath', 'workspacePaths']
print PreInvocation ['artifactDirectoryPath', 'conversationId', 'initialNumSteps', 'invocationNum', 'modelName', 'transcriptPath', 'workspacePaths']
print PostInvocation ['artifactDirectoryPath', 'conversationId', 'initialNumSteps', 'invocationNum', 'modelName', 'transcriptPath', 'workspacePaths']
print Stop ['artifactDirectoryPath', 'conversationId', 'error', 'executionNum', 'fullyIdle', 'modelName', 'terminationReason', 'transcriptPath', 'workspacePaths']
```

Per turn the capture hook received, in order, `PreInvocation(invocationNum 0)`,
`PreToolUse(stepIdx 3)`, `PostToolUse(stepIdx 3)`, `PostInvocation`,
`PreInvocation(invocationNum 1)`, `PostInvocation`, `Stop(terminationReason
NO_TOOL_CALL)`. Hook cwd `~/.gemini/config`; env
`ANTIGRAVITY_CONVERSATION_ID=<CONVERSATION_ID>`.

Daemon receipt for the print-mode shell `PreToolUse` (`daemon-receipts.jsonl`, the
fields that prove delivery; the full line is in the file):

```
$ python3 -c "import json; r=[r for r in map(json.loads, open('daemon-receipts.jsonl')) if r['mode']=='print' and r['event']=='PreToolUse' and r['tool_class']=='shell'][0]; print(json.dumps({k: r[k] for k in ('mode','event','tool_class','tool_name','ghook_command','daemon_http_status','daemon_response','ghook_stdout','ghook_exit')}))"
{"mode": "print", "event": "PreToolUse", "tool_class": "shell", "tool_name": "run_command", "ghook_command": "ghook --gobby-owned --cli=agy --type=PreToolUse", "daemon_http_status": 200, "daemon_response": {"decision": "allow"}, "ghook_stdout": {"decision": "allow"}, "ghook_exit": 0}
```

plus `ghook_request.path` `/api/hooks/execute`, `payload.source` `agy`,
`daemon_processed_marker.status` `processed`, and `daemon_hooks_log_line`
`2026-08-22 05:45:44 - WARNING - hooks.broadcaster.broadcast_event - Failed to broadcast event HookEventType.BEFORE_TOOL: 2 validation errors for PreToolUseInput`.

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

Interactive: `pane-captures/1.1.5-interactive-builtin.txt`,
`1.1.5-interactive-shell.txt`, `1.1.5-interactive-mcp.txt`.

### 1.1.6 — stream-json shape

Print (`evidence/1.1.6-print-stream.txt`, the pass-3 `run: ls -la` turn; the first-run
`list the files` turn precedes it in the same file):

```
$ agy -p "run: ls -la" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir "$PWD" --model gpt-oss-120b-medium
{"event":"init","conversation_id":"<CONVERSATION_ID>","init":{"model":"gpt-oss-120b-medium","cwd":"<WORKSPACE>","tools":["ask_custom_permission","ask_permission","ask_question","browser_click_element","browser_drag_pixel_to_pixel","browser_get_dom","browser_get_network_request","browser_input","browser_list_network_requests","browser_mouse_down","browser_mouse_up","browser_move_mouse","browser_press_key","browser_refresh_page","browser_resize_window","browser_scroll","browser_scroll_dom","browser_select_option","browser_subagent","call_mcp_tool","capture_browser_console_logs","capture_browser_screenshot","click_browser_pixel","command_status","define_subagent","delete_knowledge","execute_browser_javascript","find_by_name","finish","generate_image","grep_search","invoke_subagent","list_browser_pages","list_dir","list_permissions","list_resources","manage_inbox","manage_subagents","manage_task","multi_replace_file_content","notebook_edit","notebook_execution","open_browser_url","read_browser_page","read_resource","read_url_content","replace_file_content","run_command","schedule","search_web","sed_file","send_command_input","send_message","view_file","wait","wait_5_seconds","write_to_file"],"permission_mode":"always-proceed"}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":1,"state":"DONE","step_type":"checkpoint","duration_seconds":1.36652}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":2,"state":"DONE","step_type":"agent_response","duration_seconds":1.925638,"usage":{"input_tokens":12752,"output_tokens":221,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":12973}}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"ls -la"}}}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.205574,"tool_info":{"name":"run_command","parameters":{"CommandLine":"ls -la"},"output":"total 16\ndrwxr-xr-x@  5 <USER>  staff  160 Aug 22 06:35 .\ndrwx------@ 14 <USER>  staff  448 Aug 22 06:36 ..\n-rw-r--r--@  1 <USER>  staff  168 Aug 22 06:35 img.png\n-rw-r--r--@  1 <USER>  staff   12 Aug 22 06:35 note.txt\ndrwxr-xr-x@  2 <USER>  staff   64 Aug 22 06:35 sub\n"}}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":4,"state":"ACTIVE","step_type":"agent_response","text_delta":"Here’s the directory listing for your active workspace:\n\n```\ntotal 16\ndrwxr-xr-x@  5 <USER>  staff  160 Aug 22 06:35 .\ndrwx------@ 14 <USER>  staff  448 Aug 22 06:36 ..\n-rw-r"}}
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":4,"state":"DONE","step_type":"agent_response","text_delta":"--r--@  1 <USER>  staff  168 Aug 22 06:35 img.png\n-rw-r--r--@  1 <USER>  staff   12 Aug 22 06:35 note.txt\ndrwxr-xr-x@  2 <USER>  staff   64 Aug 22 06:35 sub\n```\n\nLet me know what you’d like to do next (e.g., view a file, edit something, run another command, etc.).\n","duration_seconds":15.431455,"usage":{"input_tokens":13092,"output_tokens":215,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":13307}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"Here’s the directory listing for your active workspace:\n\n```\ntotal 16\ndrwxr-xr-x@  5 <USER>  staff  160 Aug 22 06:35 .\ndrwx------@ 14 <USER>  staff  448 Aug 22 06:36 ..\n-rw-r--r--@  1 <USER>  staff  168 Aug 22 06:35 img.png\n-rw-r--r--@  1 <USER>  staff   12 Aug 22 06:35 note.txt\ndrwxr-xr-x@  2 <USER>  staff   64 Aug 22 06:35 sub\n```\n\nLet me know what you’d like to do next (e.g., view a file, edit something, run another command, etc.).\n","duration_seconds":21.448852,"num_turns":1,"usage":{"input_tokens":25844,"output_tokens":436,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":26280}}}
--- exit 0 ---
```

57 entries in `init.tools`. Vocabulary scan over every committed stream
(`evidence/*.txt` + `stream-json-samples.jsonl`):

```
$ python3 -c "import glob,re,collections; c=collections.Counter(m.group(1)+'='+m.group(2) for f in sorted(glob.glob('evidence/*.txt'))+['stream-json-samples.jsonl'] for line in open(f) for m in re.finditer(r'\"(step_type|event)\":\"([a-z_]+)\"', line)); print(sorted(c.items()))"
[('event=command_result', 1), ('event=init', 39), ('event=result', 46), ('event=step_update', 422), ('step_type=agent_response', 162), ('step_type=checkpoint', 34), ('step_type=error_message', 1), ('step_type=system_message', 12), ('step_type=tool', 170), ('step_type=unknown', 2), ('step_type=user_input', 41)]
```

`result.status` values observed: `SUCCESS`, `ERROR`, `CANCELED`. The >64 KiB sample
cannot exist (`evidence/1.1.6-print-big-output.txt`, the tool `output` begins with AGY's
own marker):

```
$ agy -p "run: python3 -c \"print('a'*70000)\"" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE>
"output":"<truncated 24 bytes>\naaaa     (the DONE tool step; the fixture line carries <TRUNCATED 6467 bytes> per the 4 KiB scrub rule; AGY's own cap is ~8 KiB)
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The command has run successfully. The Python one-liner printed 70,000 `'a'` characters.\n","duration_seconds":6.378778,"num_turns":1,"usage":{"input_tokens":20878,"output_tokens":631,"thinking_tokens":439,"cache_read_tokens":12199,"total_tokens":21509}}}
--- exit 0 ---
```

Interactive rendering of the same shell turn: `pane-captures/1.1.5-interactive-shell.txt`.

### 1.1.7 — sandbox / permission flags

Print (`command-captures.json` record `1.1.7 --sandbox is boolean; exit 2`,
`evidence/1.1.7-print-noflags.txt`, `evidence/1.1.6-print-stream.txt`):

```
$ agy --sandbox=bogus -p hi
invalid boolean value "bogus" for  -sandbox: strconv.ParseBool: parsing "bogus": invalid syntax
--- exit 2 ---
$ agy -p 'run: echo noflags > hello3.txt' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.093074,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo noflags \u003e hello3.txt"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"CANCELED","response":"","duration_seconds":4.822127,"num_turns":1,"usage":{"input_tokens":15688,"output_tokens":821,"thinking_tokens":658,"cache_read_tokens":0,"total_tokens":16509}}}
--- stderr ---
jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. command(<target>)). Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools.
--- exit 0 ---
```

The no-flags `init` line reads `"permission_mode":"request-review"`; with
`--dangerously-skip-permissions` it reads `"permission_mode":"always-proceed"` and the
tool runs (1.1.6 above). Interactive: `pane-captures/1.1.7-interactive-startup.txt`
(both flags accepted, prompt renders) and `1.1.7-interactive-noflags-startup.txt`;
without the skip flag the native prompt of 1.1.14 appears
(`1.1.14-interactive-permission-prompt.txt`). A hook `{"decision":"allow"}` does not
override the headless auto-deny (1.1.24 `permissionOverrides`).

### 1.1.8 — cancellation

Print (`evidence/1.1.8-print-sigint.txt`, `-sigterm.txt`; `cancel.sh <SIG>` starts the
turn in the background, waits for the `ACTIVE` tool step, lists the process tree,
signals, waits, and lists orphans after 2 s and 45 s):

```
$ bash cancel.sh INT      # agy -p 'run: sleep 40; echo finished-after-sleep' --output-format stream-json --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE> --print-timeout 3m --model gpt-oss-120b-medium & then kill -INT $PID once the tool step is ACTIVE
children of agy:
19675 python3.14
20181 zsh
sleep procs:
20182 20181 S    sleep 40
--- sent SIGINT; exit code 1 after 0s
orphans after 2s:
20182 20181 S    sleep 40
stdout lines:        6
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"sleep 40; echo finished-after-sleep"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"timeout waiting for response","duration_seconds":8.582711,"num_turns":1,"usage":{"input_tokens":12772,"output_tokens":334,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":13106}}}
--- after 45s: sleep still present?
24755 19658 S    sleep 1
$ bash cancel.sh TERM
--- sent SIGTERM; exit code 1 after 0s
orphans after 2s:
26213 26212 S    sleep 40
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"timeout waiting for response","duration_seconds":19.295677,"num_turns":1,"usage":{"input_tokens":25798,"output_tokens":671,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":26469}}}
```

The final record is byte-identical to timeout expiry (1.1.13); the tool step stays
`ACTIVE`; the `sleep 40` child outlives agy and runs to completion (only the sampler's
own `sleep 1` remains after 45 s); resume afterwards works (1.1.1). SIGINT before the
first model call is ignored and the turn completes.

Interactive (`pane-captures/1.1.8-interactive-ctrl-c.txt`,
`1.1.8-interactive-exit-armed.txt`, `1.1.8-interactive-exit.txt`):

```
> run: sleep 40; echo done-after-sleep
○ Bash(sleep 40; echo done-after-sleep) (ctrl+o to expand)
  ⎿  Interrupted · What should Antigravity CLI do instead?
  ● [05:48:19] sleep 40; echo done-after-sleep running
$ tmux send-keys -t agy-gate0 C-c; sleep 2; tmux capture-pane -p -S -200 -t agy-gate0        # at idle
press ctrl+c again to exit                                                                                                                                                         GPT-OSS 120B (Medium)
$ tmux send-keys -t agy-gate0 C-c; sleep 3; tmux capture-pane -p -S -200 -t agy-gate0; tmux list-panes -t agy-gate0 -F 'dead=#{pane_dead} status=#{pane_dead_status}'
Resume with -c (or command below):
agy --conversation=<CONVERSATION_ID>
Pane is dead (status 0, Sat Aug 22 06:39:16 2026)
dead=1 status=0
```

Hook captures for the interrupted turn: `PreInvocation`, `PreToolUse`,
`PostInvocation` — no `PostToolUse`, no `Stop` on interrupt or on exit
(`daemon-receipts.jsonl` turn 3, `turn_ended_by_stop: false`; the pass-3 exit produced
no hook file at all: the last capture before exit is that session's `Stop` for the
resume turn). `esc` mid-turn interrupts the same way. The receipt-run `ps` after the
exit showed `7350  7349  00:21 sleep 40` — the shell child is orphaned on exit too.

### 1.1.9 — network and state footprint

Print (`evidence/1.1.9-print-net.txt`; `net.sh` runs `agy -p 'run: echo netprobe'
--output-format stream-json --sandbox=false --dangerously-skip-permissions --add-dir
<WORKSPACE> --print-timeout 3m --model gpt-oss-120b-medium` in the background, samples
`lsof -nP -a -i -p` / `lsof -nP -p` over the agy process tree every 0.3 s, reverse-resolves
the remote IPs, lists `~/.gemini` files newer than a marker, and counts URL hosts in
the CLI log):

```
$ bash net.sh
--- remote-hosts ---
13.107.246.38 
142.250.100.132 yumciex-in-f132.1e100.net. 
150.171.109.183 
172.217.113.4 
172.217.114.4 
34.54.84.110 110.84.54.34.bc.googleusercontent.com. 
--- log-hosts ---
   8 daily-cloudcode-pa.googleapis.com
--- written-files ---
<AGY_APP_DATA>/bin/agentapi
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/chunks/transcript/00000000.jsonl
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/chunks/transcript_full/00000000.jsonl
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript.jsonl
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl
<AGY_APP_DATA>/cache/last_conversations.json
<AGY_APP_DATA>/cache/onboarding.json
<AGY_APP_DATA>/conversations/<CONVERSATION_ID>.db
<AGY_APP_DATA>/conversations/<CONVERSATION_ID>.db-shm
<AGY_APP_DATA>/conversations/<CONVERSATION_ID>.db-wal
<AGY_APP_DATA>/crashes/crash_35088_<CONVERSATION_ID>.log
<AGY_APP_DATA>/log/cli-20260822_034739.log
<AGY_APP_DATA>/mcp/gobby/add_mcp_server.json
<AGY_APP_DATA>/mcp/gobby/call_tool.json
<AGY_APP_DATA>/mcp/gobby/get_tool_schema.json
<AGY_APP_DATA>/mcp/gobby/get_variable.json
<AGY_APP_DATA>/mcp/gobby/import_mcp_server.json
<AGY_APP_DATA>/mcp/gobby/init_project.json
<AGY_APP_DATA>/mcp/gobby/instructions.md
<AGY_APP_DATA>/mcp/gobby/list_mcp_servers.json
<AGY_APP_DATA>/mcp/gobby/list_tools.json
<AGY_APP_DATA>/mcp/gobby/recommend_tools.json
<AGY_APP_DATA>/mcp/gobby/remove_mcp_server.json
<AGY_APP_DATA>/mcp/gobby/search_tools.json
<AGY_APP_DATA>/mcp/gobby/set_variable.json
<AGY_APP_DATA>/presence/<CONVERSATION_ID>.lock
```

`13.107.246.38` / `150.171.109.183` are `playwright{,-akamai,-verizon}.azureedge.net`
(browser-driver download attempt); the `172.217.*` / `142.250.*` addresses are
`1e100.net` Google front ends (`oauth2.googleapis.com`, `accounts.google.com`,
`play.googleapis.com`); `34.54.84.110` is `googleusercontent.com`; the model API host
in the CLI log is `daily-cloudcode-pa.googleapis.com`. Open files (`open-files` in the
same evidence file) add `~/.gemini/config/projects/*.json`, `<AGY_APP_DATA>/knowledge/
knowledge.lock`, `conversation_summaries.db`, and the login Keychain
(`security list-keychains` → `~/Library/Keychains/login.keychain-db`).

Interactive (`evidence/1.1.9-interactive-net.txt`,
`pane-captures/1.1.9-interactive-netprobe.txt`; `net-interactive.sh 150` sampled the
`agy-gate0` session's process tree for 150 s around the prompt `run: echo netprobe`):

```
$ bash net-interactive.sh 150
--- remote-hosts ---
172.217.113.4 
172.217.115.4 
172.217.116.4 
173.194.47.132 yumciaj-in-f132.1e100.net. 
34.54.84.110 110.84.54.34.bc.googleusercontent.com. 
--- log-hosts ---
  14 daily-cloudcode-pa.googleapis.com
--- process-tree ---
71810 16103 agy --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium --add-dir <WORKSPACE>
71816 71810 ~/Projects/gobby/.venv/bin/python3 ~/Projects/gobby/.venv/bin/gobby mcp-server
--- written-files ---
<AGY_APP_DATA>/bin/agentapi
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/chunks/transcript/00000000.jsonl
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/chunks/transcript_full/00000000.jsonl
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript.jsonl
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl
<AGY_APP_DATA>/conversations/<CONVERSATION_ID>.db
<AGY_APP_DATA>/history.jsonl
<AGY_APP_DATA>/log/cli-20260822_063927.log
```

Same hosts and roots; interactive mode additionally appends
`<AGY_APP_DATA>/history.jsonl` (prompt history) and keeps one `gobby mcp-server`
child for the session's lifetime.

### 1.1.10 — `RUN_COMMAND` transcript records

Print (`evidence/1.1.10-print-zero-exit.txt`, `-nonzero-exit.txt`,
`transcript-manifest.json` `zero_exit_run_command` / `nonzero_exit_run_command`):

```
$ agy -p "run: ls -la" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir "$PWD" --model gpt-oss-120b-medium
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.205574,"tool_info":{"name":"run_command","parameters":{"CommandLine":"ls -la"},"output":"total 16\ndrwxr-xr-x@  5 <USER>  staff  160 Aug 22 06:35 .\ndrwx------@ 14 <USER>  staff  448 Aug 22 06:36 ..\n-rw-r--r--@  1 <USER>  staff  168 Aug 22 06:35 img.png\n-rw-r--r--@  1 <USER>  staff   12 Aug 22 06:35 note.txt\ndrwxr-xr-x@  2 <USER>  staff   64 Aug 22 06:35 sub\n"}}}
--- exit 0 ---
$ agy -p "run: sh -c 'echo boom >&2; exit 7'" --output-format stream-json --sandbox=false --dangerously-skip-permissions --print-timeout 4m --add-dir <WORKSPACE>
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.222546,"tool_info":{"name":"run_command","parameters":{"CommandLine":"sh -c 'echo boom \u003e\u00262; exit 7'"},"output":"boom\n"}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The command exited with code 7.\n\n**Output:**\n```\nboom\n```\n","duration_seconds":4.695031,"num_turns":1,"usage":{"input_tokens":32086,"output_tokens":656,"thinking_tokens":474,"cache_read_tokens":0,"total_tokens":32742}}}
--- exit 0 ---
$ python3 -c "import json; m=json.load(open('transcript-manifest.json')); [print(json.dumps(r)) for r in m['nonzero_exit_run_command']['transcript_full']]"
{"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-08-22T08:26:18Z", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sh -c 'echo boom >&2; exit 7'", "Cwd": "<WORKSPACE>", "WaitMsBeforeAsync": 1000, "toolAction": "Running command", "toolSummary": "Execute test command"}}]}
{"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-08-22T08:26:19Z", "content": "Created At: 2026-08-22T03:26:19-05:00\nCompleted At: 2026-08-22T03:26:19-05:00\n\nThe command exited with code 7.\nOutput:\nboom\n\n"}
$ python3 -c "import json; print(json.load(open('transcript-manifest.json'))['record_type_census_1_1_18'])"
{'USER_EXPLICIT/USER_INPUT': 7, 'SYSTEM/CHECKPOINT': 6, 'MODEL/PLANNER_RESPONSE': 28, 'MODEL/GENERIC': 20, 'SYSTEM/SYSTEM_MESSAGE': 1}
```

The PostToolUse payload for the exit-7 step carries `"error": ""`. No `RUN_COMMAND`
record and no `exit_code` field exist; the 1.1.10-era `RUN_COMMAND` record is kept in
the manifest (`run_command_record_1_1_10_for_delta`) for the delta.

Interactive (`pane-captures/1.1.10-interactive-nonzero-exit.txt`,
`evidence/1.1.22-interactive-layout.txt`):

```
> run: sh -c 'echo boom >&2; exit 7'
● Bash(sh -c 'echo boom >&2; exit 7') (ctrl+o to expand)
  The command finished with exit code 7 and wrote “boom” to stderr.
--- record containing 'exit code 7':
{"step_index": 7, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-08-22T11:47:05Z", "content": "Created At: 2026-08-22T06:47:05-05:00\nCompleted At: 2026-08-22T06:47:06-05:00\n\nThe command exited with code 7.\nOutput:\nboom\n\n"}
```

Same free-text `MODEL/GENERIC` shape in both modes.

### 1.1.11 — outcome table

The deliverable is the contract-outcome table above. Census of that table
(record, modes, verdict), run against this file:

```
$ python3 - <<'EOF'
import re, pathlib
rows = [l for l in pathlib.Path("tests/fixtures/provider_contracts/agy/README.md").read_text().splitlines() if re.match(r"^\| 1\.1\.\d+ ", l)]
print(len(rows))
for l in rows:
    print(" | ".join(c.strip() for c in l.split("|")[1:4]))
EOF
24
1.1.1 resume | both | **re-confirmed unchanged** (+1 delta)
1.1.2 transcriptPath | both | **re-confirmed, layout disproven**
1.1.3 cwd remedy | both | **confirmed, remedy named**
1.1.4 image input | both | **negative**
1.1.5 payloads | both | **confirmed (hook→daemon receipts in both modes)**
1.1.6 stream-json | both | **re-confirmed unchanged** (+2 deltas)
1.1.7 sandbox flags | both | **re-confirmed unchanged**
1.1.8 cancellation | both | **confirmed**
1.1.9 network/roots | both | **confirmed**
1.1.10 RUN_COMMAND | both | **disproven**
1.1.11 outcome table | command | **confirmed**
1.1.12 controlled-tool bridge | both | **confirmed (supported)**
1.1.13 `--print-timeout` | both | **re-confirmed unchanged** (+2 deltas)
1.1.14 terminal plan menu | both | **confirmed**
1.1.15 auth footprint | both | **confirmed**
1.1.16 compaction | both | **negative**
1.1.17 interactive dispatch | both | **confirmed**
1.1.18 `--input-format stream-json` | print | **confirmed**
1.1.19 usage/quota | command | **confirmed; `/credits` negative**
1.1.20 models | command | **disproven (placement), shape confirmed**
1.1.21 `/hooks` | command | **confirmed**
1.1.22 transcript layout | both | **confirmed**
1.1.23 `--mode` | both | **confirmed**
1.1.24 response fields | both | **confirmed with negatives**
```

Revisions applied to plan §2.2, §2.3, §2.5, §4.1, §4.2, §5.1, §5.2, §5.3, §6.1,
§6.2, §6.3, §6.4, §7.1 (plan §1.2 Run 2).

### 1.1.12 — controlled-tool bridge

Print (`evidence/1.1.12-print-deny.txt`; capture hook answering
`{"decision":"deny","reason":"gate0: tool not in allowed set"}` on `PreToolUse`):

```
$ agy -p 'run: echo denied-probe' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m --dangerously-skip-permissions
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ERROR","step_type":"tool","tool_name":"run_command","duration_seconds":0.149579,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo denied-probe"},"error":{"type":"TOOL_ERROR","message":"tool call denied by pre-tool hook: gate0: tool not in allowed set"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"The tool call to run the command was denied by the environment's pre-tool safety hook:\n\n```\ntool call denied by pre-tool hook: gate0: tool not in allowed set\n```\n","error":"tool call denied by pre-tool hook: gate0: tool not in allowed set","duration_seconds":4.302956,"num_turns":1,"usage":{"input_tokens":24100,"output_tokens":1056,"thinking_tokens":859,"cache_read_tokens":8131,"total_tokens":25156}}}
--- exit 0 ---
$ agy mcp list
NAME   TYPE   STATUS   COMMAND/URL
gobby  stdio  enabled  ~/Projects/gobby/.venv/bin/gobby mcp-server
--- exit 0 ---
```

No PostToolUse capture for the denied step. The MCP PreToolUse payload
(`hook-payloads.jsonl`, record `1.1.5/1.1.17 mcp`) reads
`"toolCall":{"name":"call_mcp_tool","args":{"ServerName":"gobby","ToolName":"list_mcp_servers","Arguments":{}}}`,
so per-tool denial keys on `args.ToolName`.

Interactive (`pane-captures/1.1.24-interactive-deny.txt`, same hook answer,
`hook-payloads.jsonl` record `1.1.24 interactive PreToolUse decision=deny`):

```
> run: echo denied-probe
● Bash(echo denied-probe) (ctrl+o to expand)
  The requested command was blocked by a security policy, so it couldn’t be executed. If you’d like to run a different command (or need help with something else), just let me know!
```

Hook sequence for that turn: `PreInvocation`, `PreToolUse` (answered deny),
`PostInvocation`, `PreInvocation`, `PostInvocation`, `Stop NO_TOOL_CALL` — no
`PostToolUse`, as in print mode.

### 1.1.13 — `--print-timeout`

Print (`command-captures.json` records `1.1.13 …`, `evidence/1.1.13-print-timeout.txt`):

```
$ agy --print-timeout banana -p hi
invalid value "banana" for flag -print-timeout: time: invalid duration "banana"
--- exit 2 ---
$ agy --print-timeout 0 -p hi --output-format json
{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"timeout waiting for response","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}
--- exit 1 ---
$ agy --print-timeout 2562047h -p 'reply with exactly: ok' --output-format json --sandbox=false --add-dir <WORKSPACE> --model gpt-oss-120b-medium
{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"ok\n","duration_seconds":11.359718,"num_turns":1,"usage":{"input_tokens":12764,"output_tokens":62,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":12826}}
--- exit 0 ---
$ agy -p 'run: sleep 25; echo done' --print-timeout 8s --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE>
--- stderr ---
Error: timeout waiting for response
--- exit 1 ---
$ agy -p 'run: sleep 25; echo done' --output-format stream-json --print-timeout 8s --sandbox=false --dangerously-skip-permissions --add-dir <WORKSPACE>
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"timeout waiting for response","duration_seconds":7.023146,"num_turns":1,"usage":{"input_tokens":19990,"output_tokens":821,"thinking_tokens":637,"cache_read_tokens":12194,"total_tokens":20811}}}
--- exit 1 ---
```

Whole-turn clock; no disable sentinel (`0` expires immediately, `2562047h` is the
effectively-unbounded form); mid-stream expiry leaves the tool `ACTIVE` and its shell
child running; per turn under `--input-format stream-json` (1.1.18 `idle`).

Interactive (`pane-captures/1.1.13-interactive-print-timeout.txt`; session launched
with `agy --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium
--add-dir <WORKSPACE> --print-timeout 8s`):

```
> run: sleep 25; echo done
  The background task has finished successfully:
    done
```

The flag is accepted and inert interactively: the 25 s sleep ran as a background task
and the turn completed (hook captures for label `1.1.13 interactive --print-timeout
8s session` span 12:05:0x–12:05:4x, no error).

### 1.1.14 — terminal plan menu and keystrokes

`pane-captures/1.1.14-interactive-*.txt` (eight captures: `shortcuts`, `slash`,
`shift-tab`, `plan-mode`, `artifact-review`, `after-approve`, `permission-prompt`,
`after-allow`):

```
$ tmux send-keys -t agy-gate0 BTab          # shift+tab, three times
esc to cancel                                                                                                                                                       accept-edits · GPT-OSS 120B (Medium)
esc to cancel                                                                                                                                                               plan · GPT-OSS 120B (Medium)
esc to cancel                                                                                                                                                                      GPT-OSS 120B (Medium)
> /plan create a file plan1.txt containing the word hi
● Create(~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/plan_create_plan1_txt.md) (ctrl+o to expand)
  Artifact: plan_create_plan1_txt.md
$ tmux send-keys -t agy-gate0 C-r           # or /artifact
Action required (1 left)
› □ new plan_create_plan1_txt.md   open  approve reject
Keyboard: ↑/↓ Navigate  y/n Approve/reject  shift+a Approve all  p Preview  ctrl+g open in editor  esc Done
$ tmux send-keys -t agy-gate0 y
  ⎿  Review submitted
> [Approved] plan_create_plan1_txt.md
$ tmux send-keys -t agy-gate0 '?'
shift+tab  Cycle mode · ctrl+r  Review artifact · ctrl+o  Toggle trajectory view · esc Close
> run: echo permtest                        # session without --dangerously-skip-permissions
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
(`1.1.14-interactive-after-allow.txt`); `4` yields `⎿  User declined the tool call`
(`1.1.24-interactive-permoverride-after-no.txt`). The print half of the plan contract is
1.1.23 (`--mode plan`, `evidence/1.1.23-print-mode.txt`).

### 1.1.15 — authentication footprint

Print (`evidence/1.1.15-print-auth.txt`, `evidence/1.1.20-print-models.txt`):

```
$ security find-generic-password -s antigravity 2>&1 | head -3; security dump-keychain 2>/dev/null | awk '/antigravity|gemini|Antigravity/' | sort -u | head
    "acct"<blob>="antigravity"
    "svce"<blob>="gemini"
$ env -i HOME=~ PATH=/usr/bin:/bin:~/.local/bin ~/.local/bin/agy -p '/usage' --output-format json --print-timeout 1m
{"conversation_id":"","status":"SUCCESS","response":"Gemini Models\tWeekly Limit Remaining\t0%\t2026-08-27T08:53:33Z\nClaude and GPT models\tWeekly Limit Remaining\t99%\t2026-08-29T08:43:38Z\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"usage","data":{"description":"Within each group, models share a weekly limit. Quota is consumed proportionally to the cost of the tokens. Thus, limits will last longer with shorter tasks or using more cost-effective models. Your weekly limit is tied directly to your individual tier.","groups":[{"name":"Gemini Models","description":"Models within this group: Gemini Flash, Gemini Pro","buckets":[{"id":"gemini-weekly","name":"Weekly Limit Remaining","description":"You have hit your weekly limit, it refreshes in 5 days. If on a supported paid plan, you can use AI credits in the interim or upgrade to a higher tier.","window":"weekly","remaining_fraction":0,"reset_time":"2026-08-27T08:53:33Z"}]},{"name":"Claude and GPT models","description":"Models within this group: Claude Opus, Claude Sonnet, GPT-OSS","buckets":[{"id":"3p-weekly","name":"Weekly Limit Remaining","description":"You have used some of your weekly limit, it will fully refresh in 6 days, 23 hours.","window":"weekly","remaining_fraction":0.994983971118927,"reset_time":"2026-08-29T08:43:38Z"}]}]}}}
--- exit 0 ---
$ env GOOGLE_API_KEY=<REDACTED> GEMINI_API_KEY=<REDACTED> GOOGLE_APPLICATION_CREDENTIALS=/nonexistent/creds.json ~/.local/bin/agy -p 'reply with exactly: ok' --output-format json --print-timeout 2m --model gpt-oss-120b-medium --add-dir <WORKSPACE>
{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"ok\n","duration_seconds":17.139128,"num_turns":1,"usage":{"input_tokens":12762,"output_tokens":27,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":12789}}
--- exit 0 ---
$ env HOME=<PROBE_SCRATCH>/fakehome ~/.local/bin/agy -p '/usage' --output-format json --print-timeout 1m
{"conversation_id":"","status":"ERROR","response":"","error":"authentication failed or timed out","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}
--- stderr ---
  https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=<REDACTED>&code_challenge=<REDACTED>&code_challenge_method=S256&prompt=consent&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.profile+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcclog+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fexperimentsandconfigs+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Faicode+openid&state=<REDACTED>
Waiting for authentication (timeout 60s)...
Error: authentication timed out.
--- exit 1 ---
$ H=$(mktemp -d); HOME="$H" agy --output-format json models < /dev/null; echo "rc=$?"; ls -R "$H" | head -20
rc=1
--- stderr ---
Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
```

Credential = the login Keychain item (`svce=gemini`, `acct=antigravity`); the
scrubbed-environment launch authenticates through it (CLI log: `ChainedAuth:
authenticated via keyring`); no credential env var is accepted; no auth-CLI inference
is needed. The two unauthenticated shapes differ by surface: a `-p` *turn* (or `/usage`)
under a foreign HOME opens the OAuth prompt and fails after 60 s, while the `models`
subcommand exits 1 immediately with empty stdout and `Please sign in` (1.1.20).

Interactive (`pane-captures/1.1.15-interactive-isolated-home.txt`,
`1.1.15-interactive-empty-home.txt`):

```
$ H=$(mktemp -d); tmux new-session -d -s agy-unauth -x 200 -y 50 -c <WORKSPACE> "HOME=$H agy --model gpt-oss-120b-medium; echo \"agy exited rc=\$?\"; sleep 600"
 Welcome to the Antigravity CLI. You are currently not signed in.
 Select login method:
 > 1. Google OAuth
   2. Use a Google Cloud project
 [Use arrow keys to navigate, Enter to select]
$ tmux send-keys -t agy-unauth C-c          # nothing selected; real Keychain and ~/.gemini untouched
press ctrl+c again to exit
$ tmux new-session -d -s agy-unauth -x 200 -y 50 -c <WORKSPACE> "HOME= agy --model gpt-oss-120b-medium; echo \"agy exited rc=\$?\"; sleep 600"
Failed to start: $HOME is not defined: $HOME is not defined
agy exited rc=1
```

The isolated HOME gained only `Library/Caches/ms-playwright-go/1.57.0`; `security
find-generic-password -s gemini` afterwards still lists `acct=antigravity`,
`svce=gemini`.

### 1.1.16 — compaction signaling

Scan of every committed stream-json line (print; all first-run turns including the
87 s, 301 k-token `force_continue` turn with 46 model invocations and the
11-continuation `Stop continue` turn) and of the interactive transcript census:

```
$ python3 -c "import glob,re,collections; c=collections.Counter(m.group(1)+'='+m.group(2) for f in sorted(glob.glob('evidence/*.txt'))+['stream-json-samples.jsonl'] for line in open(f) for m in re.finditer(r'\"(step_type|event)\":\"([a-z_]+)\"', line)); print(sorted(c.items()))"
[('event=command_result', 1), ('event=init', 39), ('event=result', 46), ('event=step_update', 422), ('step_type=agent_response', 162), ('step_type=checkpoint', 34), ('step_type=error_message', 1), ('step_type=system_message', 12), ('step_type=tool', 170), ('step_type=unknown', 2), ('step_type=user_input', 41)]
$ python3 -c "import json; m=json.load(open('transcript-manifest.json')); print(m['record_type_census_1_1_18']); print(m['interactive_layout_pass3']['record_type_census'])"
{'USER_EXPLICIT/USER_INPUT': 7, 'SYSTEM/CHECKPOINT': 6, 'MODEL/PLANNER_RESPONSE': 28, 'MODEL/GENERIC': 20, 'SYSTEM/SYSTEM_MESSAGE': 1}
census: {'USER_EXPLICIT/USER_INPUT': 16, 'SYSTEM/CHECKPOINT': 1, 'MODEL/PLANNER_RESPONSE': 44, 'MODEL/GENERIC': 10, 'SYSTEM_SDK/USER_INPUT': 1, 'SYSTEM_SDK/EPHEMERAL_MESSAGE': 1, 'SYSTEM/SYSTEM_MESSAGE': 14, 'SYSTEM/ERROR_MESSAGE': 1}
```

No `step_type`, `event`, or transcript record type names compaction or context
pressure in either mode. The sole adjacent signal is the `checkpoint` step that fires
at step 1–2 of every conversation, even single-turn:

```
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":1,"state":"DONE","step_type":"checkpoint","duration_seconds":1.36652}}
```

Interactive long turns without any compaction marker:
`pane-captures/1.1.24-interactive-stop-continue.txt` (11 model invocations in one turn)
and `1.1.24-interactive-force-continue.txt`.

### 1.1.17 — interactive dispatch

Receipt run, one tmux session (`pane-captures/1.1.17-interactive.txt`,
`1.1.5-interactive-*.txt`):

```
$ tmux new-session -d -s agy-gate0 -x 200 -y 50 -c <WORKSPACE> "agy --sandbox=false --dangerously-skip-permissions --model gpt-oss-120b-medium --add-dir <WORKSPACE>"
$ tmux send-keys -t agy-gate0 -l 'list the files in this directory'; tmux send-keys -t agy-gate0 Enter      # then 'run: ls -la', then the MCP prompt
$ tmux capture-pane -p -S -200 -t agy-gate0
$ python3 -c "import json; rows=[r for r in map(json.loads, open('hook-payloads.jsonl')) if r['record'].startswith('1.1.5 daemon-receipt run (shell')]; keys=lambda m: {(r['event'], tuple(sorted(r['payload']))) for r in rows if r['mode']==m}; print(keys('print')==keys('interactive'), sorted(e for e,_ in keys('interactive')))"
True ['PostInvocation', 'PostToolUse', 'PreInvocation', 'PreToolUse', 'Stop']
```

`daemon-receipts.jsonl` interactive lines: turn 0 (`list_dir`), turn 1
(`run_command`), turn 2 (`view_file` + `call_mcp_tool`), turn 3 (interrupted
`run_command`). Payload key sets per event are identical to print mode (the
comparison above). Negatives in both modes: no `PostToolUse` for a `TOOL_ERROR`
step, no `Stop` on interrupt/exit. The print half of the same three prompts is
`evidence/1.1.6-print-stream.txt` and the `1.1.5 daemon-receipt run (…, turn 0)`
print lines.

### 1.1.18 — `--input-format stream-json`

`evidence/1.1.18-print-input-format.txt`; `inputfmt.py <case>` drives
`agy --input-format stream-json --output-format stream-json --sandbox=false
--dangerously-skip-permissions --add-dir <WORKSPACE> --print-timeout 2m --model
gpt-oss-120b-medium` over a pipe (`--conversation <id>` for `conv`, `--print-timeout
20s` for `idle`, `-p` appended for `shapes`), logs each stdin line (`>>`) and each
stdout record with a timestamp:

```
$ python3 inputfmt.py eof
[   0.0] >> {"event": "user", "message": {"content": "reply with exactly: before-eof"}}
[   2.4] init {"model": "gpt-oss-120b-medium", "cwd": "<WORKSPACE>", "permission_mode": "always-proceed"}
[   6.2] result {"conversation_id": "<CONVERSATION_ID>", "status": "SUCCESS", "duration_seconds": 3.528411, "num_turns": 1, "usage": {"input_tokens": 12103, "output_tokens": 39, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 12142}}
[   6.3] closing stdin (EOF)
[   6.4] exit code 0
$ python3 inputfmt.py conv <CONVERSATION_ID>
[   4.0] result {"conversation_id": "<CONVERSATION_ID>", "status": "SUCCESS", "duration_seconds": 901.527079, "num_turns": 3, "usage": {"input_tokens": 37926, "output_tokens": 180, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 38106}}
[   4.1] >> {"event": "user", "message": {"content": "what did you reply last time? one word"}}
[  47.6] result {"conversation_id": "<CONVERSATION_ID>", "status": "SUCCESS", "duration_seconds": 945.000829, "num_turns": 4, "usage": {"input_tokens": 51024, "output_tokens": 242, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 51266}}
[  47.7] exit code 0
$ python3 inputfmt.py idle            # --print-timeout 20s, 25 s idle between turns
alive after idle: True
[  41.8] result {"conversation_id": "<CONVERSATION_ID>", "status": "SUCCESS", "duration_seconds": 38.644318, "num_turns": 2, "usage": {"input_tokens": 24928, "output_tokens": 90, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 25018}}
$ python3 inputfmt.py cancel
[  20.8] step_update {"conversation_id": "<CONVERSATION_ID>", "step_index": 3, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "sleep 30; echo slept"}}}
[  21.1] sending SIGINT to agy pid 88685
[  21.2] result {"conversation_id": "<CONVERSATION_ID>", "status": "ERROR", "error": "context canceled", "duration_seconds": 18.736567, "num_turns": 1, "usage": {"input_tokens": 12100, "output_tokens": 362, "thinking_tokens": 0, "cache_read_tokens": 0, "tot
[  24.2] alive after SIGINT: False rc=1
$ python3 inputfmt.py shapes          # launch with -p
process exited rc=2
flag needs an argument: -p
$ python3 inputfmt.py shapes2         # raw text line on stdin
[   2.8] result {"conversation_id": "<CONVERSATION_ID>", "status": "ERROR", "error": "failed to decode stream input: invalid character 'r' looking for beginning of value", "duration_seconds": 0, "num_turns": 0, "usage": {"input_tokens": 0, "output_tokens": 
process exited rc=1
$ python3 inputfmt.py shapes3         # {"prompt": ...} without "event"
[   2.3] result {"conversation_id": "<CONVERSATION_ID>", "status": "ERROR", "error": "stream input message is missing the \"event\" field", "duration_seconds": 0, "num_turns": 0, "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_
process exited rc=1
$ python3 inputfmt.py shapes4         # bogus event, then text, then content blocks
[   0.0] >> {"event": "bogus_event"}
[  60.1] >> {"event": "user", "message": {"content": "reply with exactly: beta"}}
[  65.9] result {"conversation_id": "<CONVERSATION_ID>", "status": "SUCCESS", "duration_seconds": 5.568657, "num_turns": 1, "usage": {"input_tokens": 12762, "output_tokens": 49, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 12811}}
[  65.9] >> {"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": "reply with exactly: gamma"}]}}
[  66.9] result {"conversation_id": "<CONVERSATION_ID>", "status": "SUCCESS", "duration_seconds": 6.628119, "num_turns": 2, "usage": {"input_tokens": 25585, "output_tokens": 75, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 25660}}
--- stderr ---
warning: ignoring unsupported stream input message event "bogus_event"
```

One `result` per turn; EOF → exit 0 with no extra record; conversation id continuous
across stdin messages; `--conversation` accepted (first result `num_turns` 3); the
`--print-timeout` clock is per turn (alive after a 25 s idle under `20s`); SIGINT
kills the process with `context canceled`, exit 1 — no in-flight cancel survives.
Print-only surface: stdin transport has no interactive counterpart.

### 1.1.19 — usage / quota / credits

`evidence/1.1.19-print-usage.txt`, `command-captures.json` records `1.1.19 …`:

```
$ agy -p "/usage" --output-format json
{"conversation_id":"","status":"SUCCESS","response":"Gemini Models\tWeekly Limit Remaining\t0%\t2026-08-27T08:53:33Z\nClaude and GPT models\tWeekly Limit Remaining\t96%\t2026-08-29T08:43:38Z\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"usage","data":{"description":"Within each group, models share a weekly limit. Quota is consumed proportionally to the cost of the tokens. Thus, limits will last longer with shorter tasks or using more cost-effective models. Your weekly limit is tied directly to your individual tier.","groups":[{"name":"Gemini Models","description":"Models within this group: Gemini Flash, Gemini Pro","buckets":[{"id":"gemini-weekly","name":"Weekly Limit Remaining","description":"You have hit your weekly limit, it refreshes in 4 days, 21 hours. If on a supported paid plan, you can use AI credits in the interim or upgrade to a higher tier.","window":"weekly","remaining_fraction":0,"reset_time":"2026-08-27T08:53:33Z"}]},{"name":"Claude and GPT models","description":"Models within this group: Claude Opus, Claude Sonnet, GPT-OSS","buckets":[{"id":"3p-weekly","name":"Weekly Limit Remaining","description":"You have used some of your weekly limit, it will fully refresh in 6 days, 21 hours.","window":"weekly","remaining_fraction":0.9590020179748535,"reset_time":"2026-08-29T08:43:38Z"}]}]}}}
--- exit 0 ---
$ agy -p "/quota" --output-format json
{"conversation_id":"","status":"SUCCESS","response":"Gemini Models\tWeekly Limit Remaining\t70%\t2026-08-27T08:53:33Z\nClaude and GPT models\tWeekly Limit Remaining\t100%\t2026-08-29T08:25:01Z\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"usage","data":{"description":"Within each group, models share a weekly limit. Quota is consumed proportionally to the cost of the tokens. Thus, limits will last longer with shorter tasks or using more cost-effective models. Your weekly limit is tied directly to your individual tier.","groups":[{"name":"Gemini Models","description":"Models within this group: Gemini Flash, Gemini Pro","buckets":[{"id":"gemini-weekly","name":"Weekly Limit Remaining","description":"You have used some of your weekly limit, it will fully refresh in 5 days.","window":"weekly","remaining_fraction":0.7005872130393982,"reset_time":"2026-08-27T08:53:33Z"}]},{"name":"Claude and GPT models","description":"Models within this group: Claude Opus, Claude Sonnet, GPT-OSS","buckets":[{"id":"3p-weekly","name":"Weekly Limit Remaining","window":"weekly","remaining_fraction":1,"reset_time":"2026-08-29T08:25:01Z"}]}]}}}
--- exit 0 ---
$ agy -p "/credits" --output-format json
{"conversation_id":"","status":"ERROR","response":"","error":"/credits failed: retrieving credits: no credits info found","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}
--- exit 1 ---
$ agy -p 'reply with exactly: ok' --output-format json --print-timeout 1m          # Gemini default model, bucket at 0
{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 120h9m43s.","duration_seconds":0,"num_turns":1,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}
--- exit 1 ---
```

`num_turns 0` and zero usage on `/usage` `/quota`: no agent turn, no spend. Bucket
`remaining_fraction` drops across the committed captures (1 → 0.994 → 0.959) as the
gpt-oss turns spend.

### 1.1.20 — model list

`evidence/1.1.20-print-models.txt`, `command-captures.json` records `1.1.20 …`:

```
$ agy models --output-format json
  -h      Show help
  --help  Show help
Error: flags provided but not defined: -output-format
--- exit 1 ---
$ agy --output-format json models
{"conversation_id":"","status":"SUCCESS","response":"gemini-3.7-flash-high\tGemini 3.7 Flash (High)\ngemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)\ngemini-3.7-flash-low\tGemini 3.7 Flash (Low)\ngemini-3.6-flash-high\tGemini 3.6 Flash (High)\ngemini-3.6-flash-medium\tGemini 3.6 Flash (Medium)\ngemini-3.6-flash-low\tGemini 3.6 Flash (Low)\ngemini-3.5-flash-high\tGemini 3.5 Flash (High)\ngemini-3.5-flash-medium\tGemini 3.5 Flash (Medium)\ngemini-3.5-flash-low\tGemini 3.5 Flash (Low)\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\ngemini-3.1-pro-low\tGemini 3.1 Pro (Low)\nclaude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\nclaude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\ngpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"models","data":{"models":[{"id":"gemini-3.7-flash-high","label":"Gemini 3.7 Flash (High)"},{"id":"gemini-3.7-flash-medium","label":"Gemini 3.7 Flash (Medium)"},{"id":"gemini-3.7-flash-low","label":"Gemini 3.7 Flash (Low)"},{"id":"gemini-3.6-flash-high","label":"Gemini 3.6 Flash (High)"},{"id":"gemini-3.6-flash-medium","label":"Gemini 3.6 Flash (Medium)"},{"id":"gemini-3.6-flash-low","label":"Gemini 3.6 Flash (Low)"},{"id":"gemini-3.5-flash-high","label":"Gemini 3.5 Flash (High)"},{"id":"gemini-3.5-flash-medium","label":"Gemini 3.5 Flash (Medium)"},{"id":"gemini-3.5-flash-low","label":"Gemini 3.5 Flash (Low)"},{"id":"gemini-3.1-pro-high","label":"Gemini 3.1 Pro (High)"},{"id":"gemini-3.1-pro-low","label":"Gemini 3.1 Pro (Low)"},{"id":"claude-sonnet-4-6","label":"Claude Sonnet 4.6 (Thinking)"},{"id":"claude-opus-4-6-thinking","label":"Claude Opus 4.6 (Thinking)"},{"id":"gpt-oss-120b-medium","label":"GPT-OSS 120B (Medium)"}]}}}
--- exit 0 ---
$ agy --output-format stream-json models
{"event":"command_result","command":{"name":"models","data":{"models":[{"id":"gemini-3.7-flash-high","label":"Gemini 3.7 Flash (High)"},{"id":"gemini-3.7-flash-medium","label":"Gemini 3.7 Flash (Medium)"},{"id":"gemini-3.7-flash-low","label":"Gemini 3.7 Flash (Low)"},{"id":"gemini-3.6-flash-high","label":"Gemini 3.6 Flash (High)"},{"id":"gemini-3.6-flash-medium","label":"Gemini 3.6 Flash (Medium)"},{"id":"gemini-3.6-flash-low","label":"Gemini 3.6 Flash (Low)"},{"id":"gemini-3.5-flash-high","label":"Gemini 3.5 Flash (High)"},{"id":"gemini-3.5-flash-medium","label":"Gemini 3.5 Flash (Medium)"},{"id":"gemini-3.5-flash-low","label":"Gemini 3.5 Flash (Low)"},{"id":"gemini-3.1-pro-high","label":"Gemini 3.1 Pro (High)"},{"id":"gemini-3.1-pro-low","label":"Gemini 3.1 Pro (Low)"},{"id":"claude-sonnet-4-6","label":"Claude Sonnet 4.6 (Thinking)"},{"id":"claude-opus-4-6-thinking","label":"Claude Opus 4.6 (Thinking)"},{"id":"gpt-oss-120b-medium","label":"GPT-OSS 120B (Medium)"}]}}}
--- exit 0 ---
$ agy -p "/model" --output-format json
{"conversation_id":"","status":"SUCCESS","response":"gemini-3.5-flash-high\tGemini 3.5 Flash (High)\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"model","data":{"id":"gemini-3.5-flash-high","label":"Gemini 3.5 Flash (High)","effort":"high","is_default":false}}}
--- exit 0 ---
$ agy --effort bogus -p hi
Error: invalid model selection (--model "" --effort "bogus"): invalid --effort "bogus" (valid: low, medium, high)
--- exit 1 ---
$ H=$(mktemp -d); HOME="$H" agy --output-format json models < /dev/null; echo "rc=$?"; ls -R "$H" | head -20
rc=1
Library
<ISOLATED_HOME>/Library:
Caches
<ISOLATED_HOME>/Library/Caches:
ms-playwright-go
<ISOLATED_HOME>/Library/Caches/ms-playwright-go:
1.57.0
--- stderr ---
Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
$ H=$(mktemp -d); HOME="$H" agy --output-format stream-json models < /dev/null; echo "rc=$?"; HOME="$H" agy models < /dev/null; echo "rc=$?"; ls -a "$H"; rm -rf "$H"
rc=1
rc=1
--- stderr ---
Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
Error: Please sign in to view available models. Launch the CLI without arguments to sign in.
```

The `stream-json` form emits a single `{"event":"command_result",…}` line (the
`result` line that follows it in the first-run capture is in the evidence file). No
default marker and no effort field on the list; effort is the id suffix (`valid:
low, medium, high`); the current model comes from `/model`. Unauthenticated
(isolated HOME, real Keychain and `~/.gemini` untouched): immediate exit 1, **empty
stdout** (no command envelope), stderr `Please sign in …`, no OAuth prompt and no wait.

### 1.1.21 — `/hooks` introspection

`evidence/1.1.21-print-hooks.txt` (seven captures across the three runs),
`command-captures.json` records `1.1.21 …`:

```
$ agy -p "/hooks" --output-format json                                     # before install
{"conversation_id":"","status":"SUCCESS","response":"gobby\tenabled\tPreInvocation\t-\tcommand\t~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PreInvocation\ngobby\tenabled\tPostInvocation\t-\tcommand\t~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PostInvocation\ngobby\tenabled\tPreToolUse\t*\tcommand\t~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PreToolUse\ngobby\tenabled\tPostToolUse\t*\tcommand\t~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PostToolUse\ngobby\tenabled\tStop\t-\tcommand\t~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop\n","duration_seconds":0,"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0},"command":{"name":"hooks","data":{"hooks":[{"name":"gobby","enabled":true,"source":"~/.gemini/config/hooks.json","actions":[{"event":"PreInvocation","type":"command","command":"~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PreInvocation","timeout_seconds":45},{"event":"PostInvocation","type":"command","command":"~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PostInvocation","timeout_seconds":45},{"event":"PreToolUse","matcher":"*","type":"command","command":"~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PreToolUse","timeout_seconds":45},{"event":"PostToolUse","matcher":"*","type":"command","command":"~/.gobby/bin/ghook --gobby-owned --cli=agy --type=PostToolUse","timeout_seconds":45},{"event":"Stop","type":"command","command":"~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop","timeout_seconds":45}]}]}}}
--- exit 0 ---
$ python3 -c "import json; [print([(h['name'], h['enabled'], [a.get('event') for a in h['actions']], [a.get('command') for a in h['actions']][-1]) for h in json.loads(l)['command']['data']['hooks']]) for l in open('evidence/1.1.21-print-hooks.txt') if l.startswith('{')]"
[('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
[('gate0-capture', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '<PROBE_SCRATCH>/gate0-capture.sh Stop'), ('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
[('gate0-capture', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '<PROBE_SCRATCH>/gate0-capture.sh Stop'), ('gate0-disabled', False, ['Stop'], '<PROBE_SCRATCH>/gate0-capture.sh Stop'), ('gate0-malformed', True, ['Stop'], None), ('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
[('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
[('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
[('gate0-capture', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '<PROBE_SCRATCH>/gate0-capture.sh Stop'), ('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
[('gobby', True, ['PreInvocation', 'PostInvocation', 'PreToolUse', 'PostToolUse', 'Stop'], '~/.gobby/bin/ghook --gobby-owned --cli=agy --type=Stop')]
$ cmp ~/.gemini/config/hooks.json <PROBE_SCRATCH>/hooks.json.orig && echo "hooks.json byte-identical to pre-probe copy"
hooks.json byte-identical to pre-probe copy
```

The seven captures are, in order: first run before / with / with `{"enabled": false}`
(`gate0-disabled`) and malformed (`{"Stop":[{"type":"command"}],"NotAnEvent":[]}`,
shown `enabled:true` with an action lacking `command`; `NotAnEvent` vanishes; no
warning) / after removal; pass-3 before / with / after removal. `num_turns 0` on every
line: no agent turn. Unauthenticated (token refresh blocked by the sandbox proxy,
`command-captures.json` record `1.1.21 unauthenticated …`): OAuth prompt on stderr,
exit 1 `authentication failed or timed out`.

### 1.1.22 — transcript layout

Print conversations (`transcript-manifest.json` `layout`, `record_keys_1_1_18`,
`truncation_sample`; every first-run conversation was compared the same way):

```
$ python3 -c "import json; m=json.load(open('transcript-manifest.json')); [print(k, '=>', v) for k, v in m['layout'].items()]"
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript_full.jsonl => append-only, complete: full tool-result content (tool output itself capped by AGY at ~8 KiB with a '<truncated N bytes>' marker), native-typed tool_calls[].args. The file transcriptPath names. THE PARSER INPUT.
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/transcript.jsonl => token-efficient twin: content capped at ~4 KiB with 'truncated_fields': ['content'], tool_calls[].args values JSON-string-encoded. Same step_index set.
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/.system_generated/logs/chunks/{transcript,transcript_full}/00000000.jsonl => byte-identical copies of the parent file in every probed conversation (largest 10,751 bytes); a second chunk never opened during Gate 0, so the rollover threshold is unobserved.
<AGY_APP_DATA>/conversations/<CONVERSATION_ID>.db => SQLite (+ -wal/-shm) conversation store; the 1.0.x '.pb' file no longer exists.
~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/{scratch,.user_uploaded}/ => artifact directories; plan-mode plans are written as brain/<CONVERSATION_ID>/<name>.md (+ .metadata.json).
```

Interactive conversation of the pass-3 session (`evidence/1.1.22-interactive-layout.txt`;
`layout.py <id>` lists `brain/<id>` with sizes, `cmp`s each chunk against its parent,
counts `source/type` pairs in `transcript_full.jsonl`, and prints records matching
the given needles):

```
$ python3 layout.py <CONVERSATION_ID> "exited with code 7" "exit status 1" "exit status 2" overwritten-by-hook "not in allowed set"
   56588 .system_generated/logs/chunks/transcript/00000000.jsonl
   56360 .system_generated/logs/chunks/transcript_full/00000000.jsonl
   56588 .system_generated/logs/transcript.jsonl
   56360 .system_generated/logs/transcript_full.jsonl
     992 .system_generated/messages/<CONVERSATION_ID>.json
      45 .system_generated/messages/read.json
      17 .system_generated/tasks/task-86.log
     359 .system_generated/tasks/task-88.log
cmp transcript.jsonl chunks/transcript/00000000.jsonl: identical
cmp transcript_full.jsonl chunks/transcript_full/00000000.jsonl: identical
census: {'USER_EXPLICIT/USER_INPUT': 16, 'SYSTEM/CHECKPOINT': 1, 'MODEL/PLANNER_RESPONSE': 44, 'MODEL/GENERIC': 10, 'SYSTEM_SDK/USER_INPUT': 1, 'SYSTEM_SDK/EPHEMERAL_MESSAGE': 1, 'SYSTEM/SYSTEM_MESSAGE': 14, 'SYSTEM/ERROR_MESSAGE': 1}
```

Same two files plus byte-identical chunks in both modes; the parser input is
`transcript_full.jsonl`, the file `transcriptPath` names (1.1.2). Interactive sessions
add `messages/` (inbox) and `tasks/` (background-task logs) under
`.system_generated/`, and three record types the print runs never produced:
`SYSTEM_SDK/USER_INPUT` (the `injectSteps.userMessage`, wrapped in `<USER_REQUEST>`),
`SYSTEM_SDK/EPHEMERAL_MESSAGE` (the `injectSteps.ephemeralMessage` verbatim) and
`SYSTEM/ERROR_MESSAGE` (`Error: model output error: …` after a hook-blocked tool call).
Pane for that conversation: `pane-captures/1.1.10-interactive-nonzero-exit.txt`.

### 1.1.23 — `--mode plan|accept-edits`

Print (`evidence/1.1.23-print-mode.txt`):

```
$ agy -p 'create a file hello.txt containing the word hi' --mode plan --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":7,"state":"DONE","step_type":"tool","tool_name":"write_to_file","duration_seconds":0.194504,"tool_info":{"name":"write_to_file","parameters":{"TargetFile":"~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/create_hello_txt.md"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"I will first check the contents of the workspace directory to see if there are any existing files or if it's currently empty.\nI'll search the workspace to check if `hello.txt` already exists anywhere.\nI will create the implementation plan artifact outlining the goal, proposed changes, and verification plan.\nI have created the implementation plan at [create_hello_txt.md](file://~/.gemini/antigravity-cli/brain/<CONVERSATION_ID>/create_hello_txt.md). Please review and approve the plan to proceed.\n","duration_seconds":8.624011,"num_turns":1,"usage":{"input_tokens":21445,"output_tokens":2212,"thinking_tokens":1483,"cache_read_tokens":48784,"total_tokens":23657}}}
--- exit 0 ---
$ agy -p 'create a file hello2.txt containing the word hi' --mode accept-edits --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"DONE","step_type":"tool","tool_name":"write_to_file","duration_seconds":0.200616,"tool_info":{"name":"write_to_file","parameters":{"TargetFile":"<WORKSPACE>/hello2.txt"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"I have created [hello2.txt](file://<WORKSPACE>/hello2.txt) containing the word `hi`.\n","duration_seconds":5.69549,"num_turns":1,"usage":{"input_tokens":32351,"output_tokens":1284,"thinking_tokens":990,"cache_read_tokens":0,"total_tokens":33635}}}
--- exit 0 ---
$ agy --mode bogus -p hi
--- stderr ---
warning: unrecognized --mode value "bogus" (valid: accept-edits, plan)
--- exit 0 ---
```

The `plan` init line carries `"permission_mode":"request-review","expanded_commands":[{"name":"plan","type":"system"}]`;
`accept-edits` keeps `"permission_mode":"request-review"` yet writes without
prompting; no approval record appears in the stream. Terminal:
`pane-captures/1.1.23-interactive-plan-mode.txt`,
`1.1.23-interactive-artifact-review.txt` (menu and keystrokes as in 1.1.14).

### 1.1.24 — response-field live acceptance

Each probe set the capture hook's answer (and, where stated, exit code + stderr) for
one event, then ran the turn in print mode (`evidence/1.1.24-print-*.txt`,
`hook-payloads.jsonl` records `1.1.24 …`, `mode: print`) and again in the pass-3
interactive session (`pane-captures/1.1.24-interactive-*.txt`, `hook-payloads.jsonl`
records `1.1.24 interactive …`). `$F'` below is `--output-format stream-json
--sandbox=false --add-dir <WORKSPACE> --print-timeout 3m --dangerously-skip-permissions`.

**PreToolUse `{"decision":"deny","reason":"gate0: tool not in allowed set"}`** — honored, both modes: 1.1.12 above.

**PreToolUse `{"decision":"deny_unless_prior_grant","reason":"gate0 dupg"}`** — honored, both modes:

```
$ agy -p 'run: echo dupg-probe' $F'                                                 # skip flag = prior grant
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"I ran the command `echo dupg-probe`. Here is the output:\n\n```\ndupg-probe\n```\n","duration_seconds":4.889486,"num_turns":1,"usage":{"input_tokens":11650,"output_tokens":560,"thinking_tokens":374,"cache_read_tokens":20324,"total_tokens":12210}}}
--- exit 0 ---
$ agy -p 'run: echo dupg-probe2' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m          # no skip flag
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ERROR","step_type":"tool","tool_name":"run_command","duration_seconds":0.141677,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo dupg-probe2"},"error":{"type":"TOOL_ERROR","message":"permission check failed for command \"echo dupg-probe2\": Permission denied for command(echo dupg-probe2). gate0 dupg"}}}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"The command execution failed with a permission error:\n\n```\nPermission denied for command(echo dupg-probe2). gate0 dupg\n```\n","error":"permission check failed for command \"echo dupg-probe2\": Permission denied for command(echo dupg-probe2). gate0 dupg","duration_seconds":6.683672,"num_turns":1,"usage":{"input_tokens":11496,"output_tokens":696,"thinking_tokens":510,"cache_read_tokens":20323,"total_tokens":12192}}}
--- exit 0 ---
> run: echo dupg-probe                      # interactive, skip-flag session (1.1.24-interactive-dupg-skip.txt)
● Bash(echo dupg-probe) (ctrl+o to expand)
  dupg-probe
> run: echo dupg-probe2                     # interactive, session without the two flags (1.1.24-interactive-dupg-noskip.txt)
● Bash(echo dupg-probe2) (ctrl+o to expand)
● Bash(/bin/echo dupg-probe2) (ctrl+o to expand)
● Create(~/.gobby/runtime/managed-executions/<CONVERSATION_ID>/tmp/claude...sers-<USER>-Projects-gobby/<CONVERSATION_ID>/scratchpad/ws/tmp.txt) (ctrl+o to expand)
  The command would output:
    dupg-probe2
```

Interactively without a prior grant the field denies silently — no native permission
prompt is shown, the three tool attempts produce no `PostToolUse`, and the model
answers from text.

**PreToolUse `{"decision":"allow","overwrite":{"CommandLine":"echo overwritten-by-hook"}}`** — honored, both modes:

```
$ agy -p 'run: echo original-command' $F'
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"The output of the command is:\n```\noverwritten-by-hook\n```\n","duration_seconds":5.547806,"num_turns":1,"usage":{"input_tokens":15760,"output_tokens":726,"thinking_tokens":553,"cache_read_tokens":16260,"total_tokens":16486}}}
--- exit 0 ---
> run: echo original-command                # interactive (1.1.24-interactive-overwrite.txt)
● Bash(echo overwritten-by-hook) (ctrl+o to expand)
{"step_index": 23, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-08-22T11:51:57Z", "content": "Created At: 2026-08-22T06:51:57-05:00\nCompleted At: 2026-08-22T06:51:57-05:00\n\nThe command exited with code 0.\nOutput:\noverwritten-by-hook\n\n"}
```

The print stream's `parameters` still show `"CommandLine":"echo original-command"`
while the interactive pane renders the overwritten command; in both modes the executed
command is the overwritten one (transcript record from
`evidence/1.1.22-interactive-layout.txt`).

**PreToolUse `{"decision":"allow","permissionOverrides":["command(echo permov-probe)"]}`** (no skip flag) — NOT honored, both modes:

```
$ agy -p 'run: echo permov-probe' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ERROR","step_type":"tool","tool_name":"run_command","duration_seconds":0.220112,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo permov-probe"},"error":{"type":"TOOL_ERROR","message":"permission check failed for command \"echo permov-probe\": user denied permission to run command:\necho permov-probe"}}}}
--- exit 1 ---
$ agy -p 'run: echo permov-probe-b' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m        # hook answered {"decision":"allow"}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"CANCELED","response":"","duration_seconds":2.479505,"num_turns":1,"usage":{"input_tokens":15687,"output_tokens":633,"thinking_tokens":476,"cache_read_tokens":0,"total_tokens":16320}}}
--- stderr ---
jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. command(<target>)). Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools.
--- exit 0 ---
> run: echo permov-probe                    # interactive, fresh session without the two flags; hook answered allow + permissionOverrides ["command(echo permov-probe)","command(/bin/echo permov-probe)"] (1.1.24-interactive-permoverride.txt)
● Bash(echo permov-probe) (ctrl+o to expand)
Requesting permission for:
   echo permov-probe
Do you want to proceed?
> 1. Yes
  2. Yes, and always allow in this conversation for commands that start with 'echo'
  3. Yes, and always allow for commands that start with 'echo' (Persist to settings.json)
  4. No
$ tmux send-keys -t agy-gate0 4             # (1.1.24-interactive-permoverride-after-no.txt)
  ⎿  User declined the tool call
```

The PreToolUse capture for the interactive turn (`hook-payloads.jsonl`, record
`1.1.24 interactive permissionOverrides (no skip flag)`) shows the hook answered
exactly `{"decision": "allow", "permissionOverrides": ["command(echo permov-probe)", "command(/bin/echo permov-probe)"]}`
for `CommandLine` `echo permov-probe`; the native prompt appeared anyway.

**PostInvocation `{"terminationBehavior":"terminate"}`** — honored, both modes:

```
$ agy -p 'run: echo step-one, then run: echo step-two, then run: echo step-three, one command per tool call' $F'
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"","duration_seconds":3.8798120000000003,"num_turns":1,"usage":{"input_tokens":15712,"output_tokens":397,"thinking_tokens":239,"cache_read_tokens":0,"total_tokens":16109}}}
--- exit 0 ---
> run: echo step-one, then run: echo step-two, then run: echo step-three, then summarize       # interactive (1.1.24-interactive-terminate.txt)
● Bash(echo step-one) (ctrl+o to expand)
```

Only `step-one` ran in each mode; the `Stop` payload reads `"terminationReason":
"TERMINAL_CUSTOM_HOOK"` (print: `hook-payloads.jsonl` record `1.1.24
terminationBehavior=terminate`; interactive: record `1.1.24 interactive
terminationBehavior=terminate`, hook sequence PreInvocation, PreToolUse, PostToolUse,
PostInvocation (answered terminate), Stop).

**PostInvocation `{"terminationBehavior":"force_continue"}`** — honored, both modes:

```
$ agy -p 'reply with exactly: ok' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m --dangerously-skip-permissions --print-timeout 90s
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"permission check failed for read_file \"~/.gemini/antigravity-cli\": Permission denied for read_file(~/.gemini/antigravity-cli). Matches hardcoded system protection boundary rule.","duration_seconds":87.857987,"num_turns":1,"usage":{"input_tokens":291065,"output_tokens":10254,"thinking_tokens":5297,"cache_read_tokens":1334209,"total_tokens":301319}}}
--- exit 1 ---
> reply with exactly: ok                    # interactive, override budget 4 then {} (1.1.24-interactive-force-continue.txt)
   1                                                               │ echo step-one                                                  │ step-one
   2                                                               │ (per user request to reply exactly “ok”)                       │ ok
   3                                                               │ echo step-three                                                │ step-three
  All commands completed successfully with exit code 0.
```

Print: 46 PreInvocation/PostInvocation pairs until the 90 s timeout (the model
eventually wandered into a protected path). Interactive (`hook-payloads.jsonl` record
`1.1.24 interactive terminationBehavior=force_continue`): `invocationNum` 0–4 — four
`force_continue` answers produced four further invocations (the model resumed the
terminated step-two/step-three sequence), the fifth PostInvocation answered `{}`
ended the turn with `Stop NO_TOOL_CALL`.

**PreInvocation `{"injectSteps":[{"toolCall":{"name":"run_command","args":{"CommandLine":"echo injected-tool-ran"}}}]}`** — NOT honored (fatal), both modes:

```
$ agy -p 'reply with exactly: ok' $F'
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":1,"state":"DONE","step_type":"error_message"}}
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"ERROR","response":"","error":"Agent execution terminated due to error.","duration_seconds":0,"num_turns":1,"usage":{"input_tokens":0,"output_tokens":0,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":0}}}
--- exit 1 ---
> reply with exactly: ok                    # interactive (1.1.24-interactive-inject-tool.txt)
⚠ Agent execution terminated due to error.
Error ID: <ERROR_ID>
```

CLI log (both modes): `agent executor error: pre-invocation hook: failed to inject
steps from hook jsonhook__gate0-capture_PreInvocation_0_0: unknown injected step type:
<nil>`; `Stop.terminationReason` `ERROR`; the interactive hook sequence is
`PreInvocation`, `Stop` only.

**PreInvocation `{"injectSteps":[{"userMessage":"Also append the word PINEAPPLE to your reply."},{"ephemeralMessage":"gate0 ephemeral: ignore nothing"}]}`** — honored, both modes:

```
$ agy -p 'reply with exactly: ok' $F'
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"OK PINEAPPLE\n","duration_seconds":4.005939,"num_turns":2,"usage":{"input_tokens":15766,"output_tokens":450,"thinking_tokens":446,"cache_read_tokens":0,"total_tokens":16216}}}
--- exit 0 ---
> reply with exactly: ok                    # interactive (1.1.24-interactive-inject-msgs.txt)
> Also append the word PINEAPPLE to your reply.
  ok PINEAPPLE
```

The injected user message renders as its own `>` turn in the pane and lands in the
transcript as `SYSTEM_SDK/USER_INPUT`; the ephemeral message as
`SYSTEM_SDK/EPHEMERAL_MESSAGE` (1.1.22).

**Stop `{"decision":"continue","reason":"gate0: keep going, say DONE-N with N incremented each time"}`** — honored ×10, then forced end, both modes:

```
$ agy -p 'reply with exactly: DONE-1' --output-format stream-json --sandbox=false --add-dir <WORKSPACE> --print-timeout 3m --dangerously-skip-permissions --print-timeout 2m
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"DONE-1\nDONE-2\nDONE-3\nDONE-4\nDONE-5\nDONE-6\nDONE-7\nDONE-8\nDONE-9\nDONE-10\nDONE-11\n","duration_seconds":14.356056,"num_turns":1,"usage":{"input_tokens":75393,"output_tokens":792,"thinking_tokens":757,"cache_read_tokens":105303,"total_tokens":76185}}}
--- exit 0 ---
> reply with exactly: DONE-1                # interactive (1.1.24-interactive-stop-continue.txt)
  DONE-16
$ python3 -c "import json; print([r['payload']['executionNum'] for r in map(json.loads, open('hook-payloads.jsonl')) if r['event']=='Stop' and r['record']=='1.1.24 interactive Stop decision=continue (forced end after 10)'])"
[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
```

Eleven `Stop` hooks per turn in both modes (`executionNum` 0–10), every one answered
`continue`: the first ten continuations are honored, the eleventh is ignored and the
turn ends with `terminationReason` `NO_TOOL_CALL` (the interactive model emitted more
than one `DONE-N` per continuation, reaching `DONE-16`). `Stop.decision` enum:
`stop|continue|block`.

**PreToolUse `{"decision":"allow"}` + exit 1, stderr `gate0 hook stderr message exit1`** — fail-closed, stdout ignored, both modes:

```
$ agy -p 'run: echo exit1-probe' $F'
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ERROR","step_type":"tool","tool_name":"run_command","duration_seconds":0.190843,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo exit1-probe"},"error":{"type":"TOOL_ERROR","message":"JSON hook \"jsonhook__gate0-capture_PreToolUse_0_0\" failed: command failed: exit status 1, stderr: gate0 hook stderr message exit1\n"}}}}
--- exit 0 ---
> run: echo exit1-probe                     # interactive (1.1.24-interactive-exit1.txt)
● Bash(echo exit1-probe) (ctrl+o to expand)
  The command was blocked by a pre‑tool hook (gate0) and did not execute.
$ agy -p 'run: echo exit2-probe' $F'        # exit 2, stderr `gate0 hook stderr message exit2`
{"event":"step_update","step_update":{"conversation_id":"<CONVERSATION_ID>","step_index":3,"state":"ERROR","step_type":"tool","tool_name":"run_command","duration_seconds":0.183265,"tool_info":{"name":"run_command","parameters":{"CommandLine":"echo exit2-probe"},"error":{"type":"TOOL_ERROR","message":"JSON hook \"jsonhook__gate0-capture_PreToolUse_0_0\" failed: command failed: exit status 2, stderr: gate0 hook stderr message exit2\n"}}}}
--- exit 0 ---
> run: echo exit2-probe                     # interactive (1.1.24-interactive-exit2.txt)
● Bash(echo exit2-probe) (ctrl+o to expand)
  The execution was intercepted by the gate0 hook, which returned an exit‑status 2 error and prevented the command from running.DONE-17
```

Every subsequent tool call in the print turns (`list_dir`, `find_by_name`,
`call_mcp_tool`) failed with the same message and the turn ended `result ERROR`,
exit 0. Interactively the hook captures (`hook-payloads.jsonl` records `1.1.24
interactive hook exit code 1|2`, `response.exit_code` 1 / 2) show `PreToolUse` with no
`PostToolUse`; the transcript carries a `SYSTEM/ERROR_MESSAGE` (`model output must
contain either output text or tool calls`) before the model's textual explanation
(1.1.22). The trailing `DONE-17` is history contamination from the earlier
`Stop continue` turn in the same session.

**Stop `{}` + exit 2** — ignored, both modes:

```
$ agy -p 'reply with exactly: ok' $F'
{"event":"result","result":{"conversation_id":"<CONVERSATION_ID>","status":"SUCCESS","response":"ok\n","duration_seconds":2.80102,"num_turns":1,"usage":{"input_tokens":15679,"output_tokens":26,"thinking_tokens":25,"cache_read_tokens":0,"total_tokens":15705}}}
--- exit 0 ---
> reply with exactly: ok                    # interactive (1.1.24-interactive-stop-exit2.txt)
  okok
```

The turn ends normally in both modes; the `Stop` capture records `response.exit_code`
2 and `terminationReason` `NO_TOOL_CALL`.

Two terminal-only artefacts seen during the pass-3 session, recorded for §6.1: a
survey overlay `How's the CLI experience so far? Help us improve: [1] Good [2] Fine
[3] Bad [0] Skip` appeared after some turns (dismissed with `0`; the next prompt
still went through), and a long-running shell command may be backgrounded by the model
(`Tool is running as a background task with task id: <CONVERSATION_ID>/task-86`),
in which case the turn ends before the command does.

## Negative contracts consumers must honor

- `PostToolUse` never fires for a `TOOL_ERROR` step (hook-denied, permission-denied,
  protected path, hook exit ≠ 0, runtime failure) and its `error` was `""` in every
  capture, including a shell exit 7. Tool failure is visible only in the stream (`state:
  ERROR`, `tool_info.error`) and as the turn-level `result.error`; interactively only in
  the pane text and the transcript's `PLANNER_RESPONSE.thinking`.
- `Stop` does not fire on interrupt (`C-c`, `esc`, SIGINT/SIGTERM) or on interactive
  exit; `PreInvocation` fires once per model call, not per user turn.
- Cancellation is indistinguishable from timeout in the stream, leaves shell
  children running, and `duration_seconds` is cumulative per conversation.
- `--dangerously-skip-permissions` is mandatory for any headless tool use; hook
  `allow`/`permissionOverrides` cannot substitute for it, and interactively
  `permissionOverrides` does not suppress the native prompt either.
- `--print-timeout` governs print mode only; an interactive session ignores it.
- `ghook` posts to the daemon only for a managed context (`GOBBY_PROJECT_ID` /
  `GOBBY_SESSION_ID` / `GOBBY_AGENT_RUN_ID`, a `.gobby/project.json` under a
  `workspacePaths` entry, or `project_id` in the payload); an unmanaged AGY launch
  gets the skip JSON and no daemon receipt.

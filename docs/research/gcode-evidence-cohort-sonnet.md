# gcode evidence cohorts: Sonnet at xhigh

Status: cohort 2 (Sonnet 5) finished 2026-09-15. Cohort 3 (Sonnet 5.5, #22405) finished
2026-10-04 UTC. The cohorts are reported separately and not pooled.
Owner tasks: #22391 (design), #22394 (results), #22405 (cohort 3).
Coordinator sessions: gobby#13417 (cohort 2), gobby#14550 (cohort 3).

## Background

A first cohort ran two Haiku 4.5 `default` agents on the same question: arm A was
restricted to `gcode evidence`, arm B to regular `gcode` and Explore. Findings that
shape this design:

- Arm B never delivered. It used Claude Code's built-in `SendMessage` ("parent",
  then "main") instead of `gobby-agents:send_message`, even after a nudge, and
  closed its run as `success` with a false completion claim.
- Arm A sent a 1.9K summary and pointed at its scratchpad file, which was deleted
  when the run ended. The full answer survived only as the Write tool input in
  its transcript.
- Close-out cost more than research for arm A: after answering, 16 API calls and
  10.2K of 12.9K output tokens went to writing, delivery, the feedback survey,
  the handoff reference, and `end_agent_run`.
- Arm A spent 3 of 16 evidence calls on request-format errors (top-level `limit`,
  `read.kind: "path"`, `stale_range` for an end line past EOF). About 28% of each
  evidence response is metadata.
- Accuracy: arm A invented Argon2 parameters and a `secure` cookie flag; arm B
  claimed bcrypt for passwords and session tokens and a 7-day default session.
  Both missed middleware enforcement, CLI and agent tokens, and grant routes.
- My nudge to arm B added 14 API calls and 1.04M cache-read tokens to its totals.
- `backend-developer` cannot run without a `task_id`: its `claim` step only allows
  `claim_task` and `get_task`, so it has no Bash or gcode.
- Haiku 4.5 does not accept the effort parameter; that run used Claude Code's
  default thinking budget.

## Question

Does making `gcode evidence` available change answer quality, retrieval efficiency,
and token cost for a source-bound question about this repo?

Primary outcomes: answer score against the key below, research-phase tokens, and
evidence adoption in arm A. If arm A rarely uses evidence, the arms converge; that is
a finding about adoption, not a failed run.

## Design decisions (agreed with the user)

1. Two agent definitions derived from `default`: `test-cohort-a` has evidence
   available and says so; `test-cohort-b` has evidence blocked and says it is
   unavailable. The treatment lives in the definitions; the spawn prompt is identical.
2. The Ask pipeline was blocked in both arms in cohort 2, because it delegates the
   question to a pipeline whose work is outside the agent transcript. #23055 retired
   it (`gcode ask` and the `gobby-ask` MCP server no longer exist), so cohort 3 needs
   no Ask block. `gcode evidence` survives as a CLI only; there is no MCP evidence tool.
3. Two agents per arm (four total). Cohort 2 launched them together; cohort 3
   launched them one at a time.
4. Arm A's prompt includes the verified evidence request shapes. Both arms get the
   same regular gcode command list.
5. Every factual claim must cite `path:line`; unverified points must be labeled.
6. No coverage hint. Coverage stays a measured outcome.
7. Explore is allowed in both arms; its tokens and model are reported separately.
8. No coordinator intervention during runs. An undelivered answer is a result and
   is recovered from the transcript.

## Agent definitions

Both definitions copy `src/gobby/install/shared/workflows/agents/default.yaml`
(prompts, `rule_selectors: tag:default`, no step workflow) with these changes:

| Field | test-cohort-a | test-cohort-b |
| --- | --- | --- |
| `surfaces` | `[spawn]` | `[spawn]` |
| `provider` / `model` | `claude` / `claude-sonnet-5` (cohort 3: `claude-sonnet-5-5`) | `claude` / `claude-sonnet-5` (cohort 3: `claude-sonnet-5-5`) |
| `reasoning_effort` / `reasoning_required` | `xhigh` / `true` | `xhigh` / `true` |
| `isolation` | `none` | `none` |
| `blocked_mcp_tools` | none | none |
| Bash command block | none | `gcode evidence` |
| Agent prompt addition | Evidence block A | Evidence block B |

`blocked_tools` matches whole canonical tool names only
(`src/gobby/workflows/engine/enforcement_checks.py`, `_check_agent_tool_enforcement`),
so it cannot block a Bash subcommand. The Bash block is a custom `before_tool` rule
with a `command_pattern` block effect on `Bash`, created with
`gobby-workflows:create_rule`: `cohort-block-gcode-evidence`, selected by
`test-cohort-b` only. Cohort 2 also used `cohort-block-gcode-ask` in both arms; it
is inert since #23055 and was removed from both definitions for cohort 3.

`test-cohort-b` names the rule in `workflows.rules`; explicit names are unioned with selector
matches (`src/gobby/workflows/selectors.py`, `resolve_rules_for_agent`). The rule
carries only the `user` tag, so `tag:default` sessions do not load it. The pattern
matches `gcode` at the start of a shell segment, optionally behind a path, env
assignments, `sudo`/`command`/`exec`/`xargs`, or global flags, followed by the
subcommand word. It does not match quoted mentions such as `gcode grep -F "gcode ask"`,
or subcommands like `search-content`. It was tested against 25 positive and negative
commands before the rules were created.

### Evidence block A

```
## Retrieval tools in this environment
`gcode evidence` is available: `gcode evidence --request-json '<JSON>'` searches and reads indexed source evidence.
Request shapes (unknown fields are rejected; omit "binding"):
- search: {"schema_version":1,"operation":"search","search":{"lane":"symbol|lexical_symbol|literal|regex|content|hybrid","query":"...","paths":["src"],"limit":20}}
- read range: {"schema_version":1,"operation":"read","read":{"kind":"range","path":"...","start_line":1,"end_line":80}} (an end_line past EOF clamps to the last line and returns a "range_clamped_to_end_of_file" warning; a start_line past EOF returns "invalid_selector")
- read symbol: {"schema_version":1,"operation":"read","read":{"kind":"symbol","path":"...","qualified_name":"Class.method"}}
Optional top-level "max_bytes" (default 16384).
```

Request shapes are verified against `crates/gcode/src/evidence/contracts.rs`
(`EvidenceRequest`, `EvidenceOperation`, `SearchSelector`, `ReadSelector` and their
serde attributes). Graph operations are omitted because graph backend availability
is unverified.

### Evidence block B

```
## Retrieval tools in this environment
`gcode evidence` is unavailable in this environment. Use the other gcode subcommands, Explore, and the standard tools.
```

## Spawn

For each of `test-cohort-a` ×2 and `test-cohort-b` ×2:
`gobby-agents:spawn_agent(agent=<name>, parent_session_id=<coordinator>, prompt=<shared prompt>)`
with no provider, model, or effort overrides. A run counts only if the spawn result
reports `reasoning.effective_effort == "xhigh"`. Record `run_id`, `child_session_id`,
launch time, and `git rev-parse HEAD`.

### Shared prompt

```
Answer this question: "How does the web ui auth work in this repo? Supply your answer in markdown and send it back to me."

Use the retrieval tools available to you as you judge best, including the Explore subagent. Common gcode commands: gcode search-symbol "Name"; gcode grep -F "literal" [PATH...] -m 50; gcode grep -w "identifier" -m 50; gcode search-content "text"; gcode search "concept"; gcode outline <file>; gcode symbol-at <file>:<line>. Do not use the Ask pipeline (gcode ask or the gobby-ask Ask-run tools).

Answer requirements:
- Cite path:line for every factual claim, from source you read in this session. Mark anything you did not verify as unverified.
- Read-only: do not write files (including /tmp or your scratchpad) and do not create or claim tasks.

Delivery (follow exactly):
1. Gobby tools go through the MCP proxy. If mcp__gobby__get_tool_schema or mcp__gobby__call_tool are deferred, load them with ToolSearch "select:mcp__gobby__get_tool_schema,mcp__gobby__call_tool". Claude Code's built-in SendMessage and ListAgents cannot reach me; do not use them.
2. Call get_tool_schema(server_name="gobby-agents", tool_name="send_message"), then call_tool(server_name="gobby-agents", tool_name="send_message", arguments={"target": "parent", "content": <your full markdown answer>}). There is no size limit on content. Send the complete answer in one message; do not summarize it, split it, point to a file, or put it in end_agent_run.
3. Finish: gobby-sessions:feedback (up to 3 real friction observations about your retrieval tools, or []), load gobby:references/sessions/handoffs.md, then gobby-agents:end_agent_run. A "Connection closed" reply from end_agent_run is expected.
```

The shared prompt is unchanged from cohort 2 for comparability. Its Ask sentence now
names tools that no longer exist; that is harmless and identical across arms.

## Answer collection

- `send_message` has no content-size check (`src/gobby/mcp_proxy/tools/agent_messaging.py`)
  and `create_message` stores content untruncated
  (`src/gobby/storage/inter_session_messages.py`).
- Messages over 2,000 characters arrive in the coordinator's context as a pointer
  (`src/gobby/hooks/pending_messages.py:10-11,115-122`). Fetch each answer with
  `gobby-agents:get_inter_session_message(message_id=...)`, which is exempt from
  result offload (`src/gobby/mcp_proxy/services/result_offload.py:30-35`), and save
  it before grading.
- Do not grade from `get_agent_result`: sending to the parent writes the run
  `result`, but `end_agent_run` overwrites it with the handoff.

## Answer key

Scoring: each of 15 facts is scored correct 1, missed 0, or wrong -1. Claims without
a citation, and citations that do not support their claim, are counted separately.

The key was kept outside the repository until every run finished. The agents ran
with isolation `none` in this checkout, and the code index serves `docs/`, so a key
in the working tree would have been retrievable evidence. Citations were rechecked
against HEAD after the runs. #22395 (dead hook override) and #22385 (GitHub
webhook prefix) each removed a line from `middleware/auth.py`, which moved its
citations.

1. `useAuth` checks `GET /api/auth/status` on mount and exposes login
   (`POST /api/auth/login`) and logout (`POST /api/auth/logout`)
   (`web/src/hooks/useAuth.ts:23,51,72`).
2. `App.tsx` renders `LoginPage` when unauthenticated (`web/src/App.tsx:473`) and
   gates the chat connection with `connectionEnabled: !authLoading && authenticated`
   (`web/src/App.tsx:132`).
3. Login rate limit: 5 failures within 5 minutes triggers a 60-second lockout
   (`src/gobby/servers/routes/auth.py:31-33`).
4. The rate-limit client key is the peer address, or a SHA256 of the Tailscale login
   header when `ui_expose == "tailscale"` and the peer is loopback
   (`src/gobby/servers/routes/auth.py`, `_login_client_id`).
5. Passwords use Argon2id with `m=65536,t=3,p=4` (`src/gobby/identity.py:15,27`),
   compared with `secrets.compare_digest` (`src/gobby/identity.py:122`); unknown
   users are checked against `DUMMY_PASSWORD_HASH`
   (`src/gobby/servers/auth_service.py`, `verify_password`).
6. Session tokens are `os.urandom(32).hex()` and stored as SHA256 hashes in
   `auth_sessions` (`src/gobby/storage/auth.py:32,126`).
7. Server-side session lifetime is 12 hours, or 30 days with remember-me
   (`src/gobby/storage/auth.py:23-24,129`).
8. Cookie `gobby_session` is `httponly` and `samesite=lax`; `max_age` (30 days) is
   set only with remember-me; no `secure` flag is set
   (`src/gobby/servers/routes/auth.py:30,149-155`).
9. Expired sessions are cleaned up on session creation and deleted on validation
   (`src/gobby/storage/auth.py:167,174`).
10. Changing a password deletes all of that user's sessions
    (`src/gobby/storage/users.py:155-170`).
11. `AuthMiddleware` is installed app-wide (`src/gobby/servers/app_factory.py:128-130`);
    public paths include `/`, `/api/health`, and the `/api/auth`, webhook, and
    `/assets` prefixes (`src/gobby/servers/middleware/auth.py:27-42`).
12. Unauthenticated requests to `/api/`, `/mcp`, or `/memory` get a 401 JSON error
    with login and CLI-token guidance; other browser routes fall through to the SPA
    shell so React renders login (`src/gobby/servers/middleware/auth.py:44-53`,
    `AuthMiddleware.dispatch`).
13. Accepted credentials, in order: `Authorization: Bearer` (local CLI token or agent
    API token), `X-Gobby-Local-Token`, then the `gobby_session` cookie
    (`src/gobby/servers/auth_service.py`, `_legacy_rejection` and `_accepted_bearer`).
14. Grant routes also require the `X-Gobby-Grant` header with identity matching
    (`forged_identity` on mismatch), and effectful routes need the daemon lease
    (409 `lease_not_held`) (`src/gobby/servers/auth_service.py`, `authenticate`).
15. `GET /api/auth/status` returns `{"authenticated": bool}` from
    `is_request_authenticated` (`src/gobby/servers/routes/auth.py`, `auth_status`).

## Metrics

Transcripts: `~/.claude/projects/-Users-josh-Projects-gobby/<child_session_id>.jsonl`.
The analyzer from the first cohort is `analyze.py` in the coordinator's scratchpad.

- Delivered in full through `send_message` (yes/no); message size.
- Wall time, API calls, tool calls and errors, peak context.
- Token totals (input, cache write, cache read, output), split into research and
  close-out at the first delivery attempt.
- Tool-result characters by tool.
- Evidence adoption: evidence calls divided by retrieval calls (arm A); any evidence
  or Ask attempts in arm B and how they were blocked.
- Explore sidechain tokens and model, reported separately.
- Answer score, uncited claims, unsupported citations.
- Friction observations the agents submit through the feedback survey.

## Cohort 2 (2026-09-15, Sonnet 5)

### Pre-launch checks

1. Create both definitions with `gobby-workflows:create_agent_definition` and pass
   `evaluate_agent`.
2. Confirm the Bash command block for `gcode evidence` (arm B) and `gcode ask`
   (both arms), scoped to these agents only.
3. Send a ~40K-character test message to the coordinator session and read it back
   by message ID.
4. Record `git rev-parse HEAD` and `gcode status` freshness at launch.

Results (2026-09-15):

1. `test-cohort-a` and `test-cohort-b` are created. `evaluate_agent` returns valid for both,
   and `evaluate_spawn` resolves `claude` / `claude-sonnet-5` / isolation `none` for both.
   The first `evaluate_agent` call for arm A hit the daemon's 30-second request timeout
   while running concurrently with arm B; it passed when rerun.
2. The rules `cohort-block-gcode-ask` and `cohort-block-gcode-evidence` are created
   (see Agent definitions).
3. A 40,100-character self-message (`9c57ca37-1b93-4e62-9fd8-7f0a0f6a5b2e`) arrived as a
   pointer and came back complete from `get_inter_session_message`, through the last line
   and end marker.
4. The code index is healthy: 7,555 files and 137,127 symbols. It was last indexed at
   15:09:08 UTC during preparation and refreshed at 15:24:59 UTC, after the answer key
   was removed from this doc. After that, `gcode search-content` and `gcode grep` over
   `docs/` returned no answer-key content.

### Launch record

The four agents were spawned concurrently on 2026-09-15 from HEAD `3a45d49c04`. Their
run `started_at` times fall between 15:28:13 and 15:30:24 UTC. Every spawn reported `reasoning.effective_effort: "xhigh"` with status
`applied`. An earlier attempt was blocked before execution by the coordinator's
`require-restraint-skill` gate, so no agent started twice.

| Arm | Run ID | Child session |
| --- | --- | --- |
| A1 | `a80d8234-9815-4f49-baa2-65f4cce479a8` | `1d694c10-bc32-4d75-aab5-a59f29a1dc34` |
| B1 | `9d54786a-4dae-4266-bc66-497e3e6968d1` | `a8756845-ee74-4b65-ba4d-71b812658e5d` |
| A2 | `91c5645a-93d2-4317-86a5-be97a1c2b9b6` | `37e84a49-9cf8-42f6-bc0e-6403c3097263` |
| B2 | `76ef0611-b522-44c4-a61a-1e2cfeac57a0` | `f23803eb-7608-4e2f-bea4-79dc73396ab5` |

### Results

Line numbers in this section refer to launch HEAD `3a45d49c04`.

#### Delivery

Two of the four runs delivered. B1 and A2 never connected to the Gobby MCP server.
Claude Code gives each MCP server 30 seconds to connect unless `MCP_TIMEOUT` is set,
and Gobby did not set it. With four concurrent spawns, `gobby mcp-server` took 25.2
seconds to connect for A1 and more than 30 seconds for A2 and B1. Without Gobby
tools, those two printed their answers in the terminal and looped on stop-hook
reprompts. The watchdog then completed both with status `success`. Their answers
were graded from transcript text.

| Arm | Run | Delivered | Answer chars | API calls | Tool calls | Minutes to first delivery attempt |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | `a80d8234` | yes | 8,283 | 43 | 70 | 25.6 |
| A2 | `91c5645a` | no | 6,977 | 47 | 68 | 19.4 |
| B1 | `9d54786a` | no | 7,990 | 49 by 16:13 UTC | 80 at completion | 24.9 |
| B2 | `76ef0611` | yes | 10,748 | 38 | 68 | 20.8 |

#### Cost

Research is everything up to the first delivery attempt. For A2 and B1 the later
phase is the loop after delivery failed.

| Arm | Research calls | Research output | Research cache read | Later calls | Later output | Peak context |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 38 | 19.7K | 3.07M | 5 | 4.0K | 119.9K |
| A2 | 28 | 11.9K | 2.26M | 19 | 7.7K | 129.2K |
| B1 | 38 | 13.3K | 2.84M | 11 by 16:13 UTC | 7.7K | 106.0K |
| B2 | 30 | 16.7K | 2.26M | 8 | 3.4K | 106.4K |

#### Retrieval

- A1 made 5 evidence range reads with no errors.
- A2 made 16 evidence calls (15 range reads, 1 symbol read), with 2 errors: one
  malformed JSON request, and `stale_range` for lines 1..200 of an 83-line file.
- Arm B made no evidence or Ask attempts. No run used Explore or Ask.

#### Scores

Partial credit counts 0.5. No answer contradicted a key fact.

| Arm | Strict | With partial credit | Partial facts | Missed facts | Wrong non-key claims | Bad citations |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 11 | 12 | 5, 9 | 10, 14 | 2 | 4 |
| A2 | 12 | 13 | 9, 14 | 10 | 0 | 5 |
| B1 | 11 | 12 | 5, 14 | 9, 10 | 1 | 2 |
| B2 | 10 | 11 | 5, 14 | 3, 4, 10 | 1 | 7 |

Wrong claims outside the key:

- A1 said `/api/hooks` is normally exempt. It was misled by the dead
  `_remote_hook_requires_auth` override, which #22395 removed.
- A1 said `authenticate` is used by both WebSocket entry points.
- B1 said `AuthMiddleware` is the outermost middleware. `CORSMiddleware` is
  (`src/gobby/servers/app_factory.py:132-141`).
- B2 said requests without credentials are allowed unless they mutate.
  `_legacy_rejection` returns a `missing_auth` 401.

All five of A2's bad citations point into files it read only through evidence. The
evidence excerpt is a JSON-escaped string with no per-line numbers, so the agent had
to count lines. Citation drift is not specific to evidence: B2's seven bad
citations came from ordinary reads.

#### Problems found

1. Claude Code's MCP connection deadline cost two of the four answers. Every Claude
   spawn, resume, and web chat now sets `MCP_TIMEOUT=120000` (#22393).
2. On purpose, the watchdog completes an idle run that has no step workflow and no
   claimed task with status `success`
   (`src/gobby/agents/watchdog/completed_turn_recovery.py`). A parent therefore sees
   success for a run that never delivered. Changing that status needs a decision.
3. Concurrent spawns overloaded the daemon. `prepare_sandbox_run_paths` took 34.8
   and 58.3 seconds, and A1 received `DAEMON_UNAVAILABLE` once.
4. `gcode evidence` range excerpts have no per-line numbers, and a request past the
   end of a file is reported as `stale_range` (`crates/gcode/src/evidence/read.rs`).
   B1 and B2 also tried `gcode search -m`, which is unsupported.
5. B2 filed duplicate feedback after a source validation error.
6. The dead `_remote_hook_requires_auth` override in
   `src/gobby/servers/middleware/auth.py` misled A1. #22395 removed it.

#### Conclusion

Evidence availability made no measurable accuracy difference. Arm A scored 11 and 12
strict and arm B scored 11 and 10, and every run missed fact 10 (a password change
revokes the user's sessions). Evidence did not reduce citation errors, and research
cost ranges overlapped between arms. Two runs per arm detect only large differences,
and half the runs lost delivery to a harness defect. Replacement runs (one per arm,
started a few minutes apart, after #22393) await the user's decision.

## Cohort 3 (2026-10-03, Sonnet 5.5)

Cohort 3 is reported on its own and is not pooled with cohort 2. Three things
changed between the cohorts, so no difference between them can be attributed to
evidence alone:

- **Model.** Cohort 2 ran `claude-sonnet-5`. Cohort 3 ran `claude-sonnet-5-5` at
  `xhigh` (Josh's pick). The definitions are `test-cohort-a`
  (`a3d9403f-0782-4192-8790-d3a685031231`) and `test-cohort-b`
  (`0db7cf60-6890-45ef-8497-dd3cc88b401f`). Each is `reasoning_required: true`,
  isolation `none`.
- **Evidence surface.** #22400 added `numbered_excerpt` with `N|` line prefixes,
  and made an `end_line` past the end of the file clamp with a
  `range_clamped_to_end_of_file` warning. Cohort 2's arm A had neither. Evidence
  block A was updated to match (see Evidence block A).
- **Ask.** #23055 retired the Ask pipeline, so neither arm needed an Ask block.

Every run used the installed `~/.gobby/bin/gcode`, sha256
`751517cbe6ef0937f6b3f43587c5873483c406e45a96011450934b17a42be5bc`, installed
2026-10-02 10:20:33 CDT, before the first launch.

### Launch record

The runs were launched serially. Each one started after the previous run had
delivered. The Lane Manager got START and END messages for B1, A2 and B2, and an
END message for A1. No run
used Explore, and the coordinator did not intervene in any run.

A1 ran in an earlier worktree at `468ac4bebd`. The other three runs ran in a fresh
worktree at `4947fc5681`. `git diff --stat 468ac4bebd..4947fc5681` lists 42 files.
None is an answer-key file or an auth source any run cited; a diff scoped to those
paths is empty. The other changes are:

- `crates/gclient/` UI and tests (14 files). Every run used the installed `gcode`
  above, so no crate source was built into a run.
- Python outside the auth surface: `agents/resume_executor.py`,
  `events/live_wake.py`, `events/wake.py`,
  `mcp_proxy/tools/tasks/_lifecycle_close.py`, `runner_init/wake_activity.py`,
  `runner_lifecycle_reconcile.py`, `tasks/tdd_evidence.py`,
  `tasks/tdd_python_evidence.py` and `tasks/transcript_tool_arguments.py`, plus
  their tests.
- Web Activity panel components (`web/src/components/activity/`), docs, the
  deploy runbook, the roster and one skill reference.

None of these touches the web UI auth path the question asks about, so A1 was not
rerun.

| Run | Arm | Run ID | Child session | Spawned (UTC) | HEAD |
| --- | --- | --- | --- | --- | --- |
| A1 | A | `d2f82316-90ed-469f-941d-10d4ae0388b8` | `263959dc-f4d5-4e60-8fb9-97cd65e53101` | 2026-10-03 20:45:19 | `468ac4bebd` |
| B1 | B | `f5d3a68d-1b6d-4f01-a1b4-f8b744db1b9d` | `bf1b79cf-47f6-4a57-a807-2f8c7741171c` | 2026-10-04 00:05:30 | `4947fc5681` |
| A2 | A | `2833a860-d299-4d49-8d12-016f38d36bb0` | `e694eb47-4941-4d5d-b0a6-8dc6da7c13a7` | 2026-10-04 00:10:11 | `4947fc5681` |
| B2 | B | `4f79d06b-1a85-42fe-9ddc-4811eae922b8` | `72f332ea-c9c8-4923-9f30-cc72ec6315e8` | 2026-10-04 00:15:53 | `4947fc5681` |

Every spawn reported `reasoning.effective_effort: "xhigh"` with status `applied`.

### Delivery

All four runs delivered. Delivery was read from the child transcripts and the
stored messages, not from run status. Each run sent its full answer to the parent in
one `send_message`. A1 then sent a second, short message: an erratum withdrawing
one claim. Every run ended with `dirty_paths=[]`.

Tool calls are the `tool_use` blocks in the transcript, blocked calls included.
Research time runs from the first prompt to the first delivery call.

| Run | Answer chars | API calls | Tool calls | Research minutes |
| --- | --- | --- | --- | --- |
| A1 | 10,991 | 24 | 35 | 4.9 |
| A2 | 12,571 | 22 | 25 | 4.9 |
| B1 | 8,811 | 20 | 57 | 3.5 |
| B2 | 10,794 | 35 | 50 | 12.6 |

### Cost

Research covers every call before the first delivery call; close-out covers that
call and everything after it.

| Run | Research calls | Research output | Research cache read | Close-out calls | Close-out output | Peak context | Sandbox violations |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | 21 | 20.5K | 1.51M | 3 | 3.2K | 121.1K | 297 |
| A2 | 17 | 19.0K | 1.37M | 5 | 2.7K | 211.7K | 282 |
| B1 | 17 | 16.6K | 1.14M | 3 | 2.3K | 109.8K | 238 |
| B2 | 30 | 24.4K | 2.69M | 5 | 2.8K | 246.0K | 679 |

A2's peak context comes from evidence reads of up to 40,000 bytes each. The sandbox
violation counts come from `~/.gobby/logs/sandbox-violations/<run_id>.jsonl` and
were not examined further.

### Retrieval

- **Arm A used evidence only to read files.** Neither A run made a search,
  symbol-read or graph request.
- **A1:** 29 range reads in 13 Bash calls: 3 direct requests and 26 through an
  `rd` shell helper.
  - It hit one error, `--max-bytes` passed as a CLI flag.
  - It requested an `end_line` past the end of the file on purpose, and got 4 clamp
    warnings.
  - It printed `excerpt` and numbered the lines itself from `line_start`. It never
    used `numbered_excerpt`.
- **A2:** 20 range reads with no errors and one clamp warning.
  - 19 of them went through an `rd` helper that piped `.items[0].numbered_excerpt`
    through `jq`.
- **Arm B made no evidence attempts**, so the block rule never fired.
  - B1 used `gcode grep`, `outline` and Read.
  - B2 relied on `gcode symbol-at` (22 uses) and `search-symbol` (9 uses).
- **Read redirects.** The hooks redirected 12 Read calls to gcode: A1 1, A2 1, B1 4
  and B2 6. Each redirect came from `require-code-index-skill` or
  `prefer-gcode-for-source-read`.
- **Key exposure.** No run searched git history or read this document. The answer
  key was removed from the tree in `0e0a6b0a71` but remains in `1ba2746600`. No run
  ran `git log` or `git show`, or mentioned `1ba2746600` or this doc.

### Scores

Graded against the same 15 facts as cohort 2, re-anchored at `468ac4bebd`; the
key files are byte-identical at `4947fc5681`. Grading used the same rubric:
correct 1, partial 0.5, missed 0, wrong -1. A fact gets full credit only when the
answer states every component of it, and partial credit when exactly one is
missing, and no credit when two or more are missing. A component counts only when
the answer states it in its own text; a citation of the line that defines it does
not. The three facts with several components are:

- Fact 5: Argon2id parameters (`m=65536, t=3, p=4`), `compare_digest` for the
  password check, and the dummy hash.
- Fact 11: app-wide middleware installation, and the public paths.
- Fact 14: the grant header, identity matching, and the lease 409.

| Run | Strict | With partial credit | Partial facts | Missed facts | Wrong non-key claims | Bad citations |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 14 | 14 | — | 10 | 1 (withdrawn by erratum) | 1 |
| A2 | 12 | 13 | 5, 14 | 10 | 0 | 0 |
| B1 | 13 | 13.5 | 11 | 5 | 0 | 2 |
| B2 | 12 | 13 | 5, 14 | 10 | 0 | 0 |

Notes on individual runs:

- **A1** first said that grant-matrix routes do not accept the browser cookie, then
  withdrew it in its erratum. Its single bad citation belongs to that claim.
- **B1's two bad citations** are both off by one line in `storage/auth.py`. It cites
  `:127` for the token (actual `:126`) and `:128` for the duration choice (actual
  `:129`).
- **B1 is the only run in either cohort that found fact 10**: a password change
  deletes the user's sessions (`storage/users.py:169-171`).
- **B2 stated the grant-route behavior correctly.** `_accepted_bearer` accepts a
  valid cookie, then `authenticate` requires `X-Gobby-Runtime-Grant` and returns
  `missing_grant` without it. B2 labeled this as its own reading of the code.
- **A2 marked one point unverified**: the final `compare_digest` step in
  `verify_password_hash`. That is why it has partial credit on fact 5.
- **Arm B on fact 5.** B2 names Argon2id, `compare_digest` and the dummy hash but
  only cites the parameter constants, so it has partial credit. B1 says "Argon2",
  cites the constants without their values, and names `compare_digest` only for the
  bearer token. With two components missing, fact 5 counts as missed for B1.

**Did per-line numbering change arm A's bad-citation count?** Cohort 2's A1 and A2
had 4 and 5 bad citations. All five of A2's sat in files it read only through
unnumbered evidence. Cohort 3's arm A had 1 and 0.

Arm B fell too, from 2 and 7 to 2 and 0. The model changed at the same time, so this
drop cannot be credited to #22400 alone. What the data does show:

- A2 read every evidence file through `numbered_excerpt` and has no bad citations.
- A1 numbered lines itself from `line_start` and miscited none of them.

### Problems found

1. **Evidence block A advertises a lane that fails.** It lists the `hybrid` search
   lane, but on this install that lane returns `semantic_identity_required`
   ("hybrid search requires a verified semantic identity"). This was checked at
   `4947fc5681`. No arm A run searched, so it affected no result. A future cohort
   should drop `hybrid` from the block or configure the semantic identity first.
2. **`get_session_messages` cannot supply the cost metrics.** It renders each child
   run as two messages: the prompt, and one assistant message that holds every tool
   chain. The tool calls are present, but that message's `usage` covers a single API
   call (B1: 581 output tokens), and there is no per-call boundary. API call counts
   and token totals therefore came from the raw Claude Code transcripts under
   `~/.claude/projects/-Users-josh--gobby-worktrees-gobby-r-22405-cohort3-468ac4/`
   (A1) and `.../r-22405-cohort3-4947fc/` (B1, A2, B2).
3. **A1 needed an erratum** to withdraw a wrong claim after delivery. The protocol
   has no slot for corrections, so the erratum arrived as a second message.

### Conclusion

Evidence availability again made no measurable accuracy difference.

- **Scores.** Arm A scored 14 and 12 strict, and arm B scored 13 and 12. With
  partial credit, arm A has 14 and 13, and arm B has 13.5 and 13.
- **Adoption.** Both arm A runs adopted evidence, but only as a file reader.
- **Cost.** Research time overlapped across arms: arm A took 4.9 minutes for both
  runs, and arm B took 3.5 and 12.6.
- **Citations.** Accuracy improved in both arms against cohort 2. Arm A had the
  fewest bad citations, but the model change confounds that.

With two runs per arm, the cohort can detect only large differences, and none
appeared.

## Known confounds

- Close-out gates (feedback survey, handoff reference, `end_agent_run`) add fixed
  cost to every run.
- Hook teaching gates fire on first raw search or read in each arm.
- The model and effort used by Explore subagents are not controlled.
- Two runs per arm detect only large differences.
- Cohort 3 changed the model (Sonnet 5 to Sonnet 5.5), the evidence surface
  (#22400) and the Ask surface (#23055) at once, so differences between cohorts
  cannot be attributed to any one of them.
- All four agents were spawned at once. The load delayed the Gobby MCP connection
  past Claude Code's 30-second deadline for two runs (fixed by #22393).

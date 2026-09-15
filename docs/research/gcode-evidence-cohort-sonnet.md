# gcode evidence cohort: Sonnet 5 at xhigh

Status: launched 2026-09-15 (see Launch record); results pending. Owner task: #22391.
Coordinator session: gobby#13417.

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
- Accuracy: both arms made factual errors and missed parts of the auth path. The
  details are held back with the answer key until the runs finish.
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
2. The Ask pipeline (`gcode ask`, `gobby-ask` Ask-run tools) is blocked in both arms,
   because it delegates the question to a pipeline whose work is outside the agent
   transcript.
3. Two agents per arm (four total), launched together.
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
| `provider` / `model` | `claude` / `claude-sonnet-5` | `claude` / `claude-sonnet-5` |
| `reasoning_effort` / `reasoning_required` | `xhigh` / `true` | `xhigh` / `true` |
| `isolation` | `none` | `none` |
| `blocked_mcp_tools` | `gobby-ask` Ask-run tools (all except `evidence`) | `gobby-ask:*` |
| Bash command block | `gcode ask` | `gcode ask`, `gcode evidence` |
| Agent prompt addition | Evidence block A | Evidence block B |

`blocked_tools` matches whole canonical tool names only
(`src/gobby/workflows/engine/enforcement_checks.py`, `_check_agent_tool_enforcement`),
so it cannot block a Bash subcommand. The Bash blocks are two custom `before_tool`
rules with a `command_pattern` block effect on `Bash`, created with
`gobby-workflows:create_rule`:

- `cohort-block-gcode-ask`: selected by both agents.
- `cohort-block-gcode-evidence`: selected by `test-cohort-b` only.

The agents name them in `workflows.rules`; explicit names are unioned with selector
matches (`src/gobby/workflows/selectors.py`, `resolve_rules_for_agent`). The rules
carry only the `user` tag, so `tag:default` sessions do not load them. The pattern
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
- read range: {"schema_version":1,"operation":"read","read":{"kind":"range","path":"...","start_line":1,"end_line":80}} (end_line must not exceed the file's length)
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
`gcode evidence` and the gobby-ask `evidence` tool are unavailable in this environment. Use the other gcode subcommands, Explore, and the standard tools.
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

The key stays outside the repository until every run finishes. The agents run with
isolation `none` in this checkout, and the code index serves `docs/`, so a key in the
working tree would be retrievable evidence. It will be added here with the results.

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

## Pre-launch checks

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

## Launch record

The four agents launched concurrently at about 15:26 UTC on 2026-09-15, from HEAD
`3a45d49c04`. Every spawn reported `reasoning.effective_effort: "xhigh"` with status
`applied`. An earlier attempt was blocked before execution by the coordinator's
`require-restraint-skill` gate, so no agent started twice.

| Arm | Run ID | Child session |
| --- | --- | --- |
| A1 | `a80d8234-9815-4f49-baa2-65f4cce479a8` | `1d694c10-bc32-4d75-aab5-a59f29a1dc34` |
| B1 | `9d54786a-4dae-4266-bc66-497e3e6968d1` | `a8756845-ee74-4b65-ba4d-71b812658e5d` |
| A2 | `91c5645a-93d2-4317-86a5-be97a1c2b9b6` | `37e84a49-9cf8-42f6-bc0e-6403c3097263` |
| B2 | `76ef0611-b522-44c4-a61a-1e2cfeac57a0` | `f23803eb-7608-4e2f-bea4-79dc73396ab5` |

## Known confounds

- Close-out gates (feedback survey, handoff reference, `end_agent_run`) add fixed
  cost to every run.
- Hook teaching gates fire on first raw search or read in each arm.
- The model and effort used by Explore subagents are not controlled.
- Two runs per arm detect only large differences.

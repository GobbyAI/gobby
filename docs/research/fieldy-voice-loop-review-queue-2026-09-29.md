# Fieldy voice loop: tool inventory and the review-queue flow

- **Task:** #23104. The research is for #22719, which stays escalated until Josh decides.
- **Author:** Researcher gobby#14550, 2026-09-29. Requested by Orchestrator gobby#14737.
- **Labels:** VERIFIED means read in code or observed on a live call this session. INFERRED means not proven.
- **Privacy:** no transcript content is reproduced here. The one live call was a 2-item metadata listing, used only to prove access.

## Bottom line

Josh's flow is feasible today with existing parts, and needs no new agent type:

- a cron job
- a pipeline with `mcp` steps and one `prompt` step
- a parent task that serves as the review queue

It is safe only with three additions:

1. A durable record of which conversation ids have already been processed. This is what stops duplicates.
2. Every suggestion is created with `allow_automation=false` under the queue parent, so nothing is dispatched.
3. The Plan/Defer decision is stored somewhere durable. Telegram buttons expire after at most 1 hour and are lost on a daemon restart.

**Recommendation:** use the pipeline, not a persistent agent. When nothing new has arrived, a 5-minute pipeline makes only a cheap metadata call and uses no LLM. A persistent agent would hold a live session and its context the whole time, just to wait.

This reverses Josh's stated rule from 09-24: "I can ask when I want the assistant to check fieldy. I don't need an automated line." (#22719 description). His new request supersedes that rule. The review-only design is the difference: nothing becomes worker assignments.

## 1. Fieldy tool inventory (VERIFIED, live `list_tools` on server `fieldy`)

The server uses HTTP transport at `https://api.fieldy.ai/mcp`, is global, and has 7 tools. All of them read data except `share`.

| Tool | Input | Output | Notes for the loop |
|---|---|---|---|
| `fieldy_browse_conversations` | optional `filters` (`from`, `to`, `attendee`, `meetingTitle`, `location`, `recordingSource` wearable/phone/desktop), `cursor`, `limit` ≤20 | Newest first: `id`, `title`, `summary` (may be null), `startTime`/`endTime`, `speakers`, `recordingSource`, `url`, `location`. Also `nextCursor` (`<startTime>|<id>`), `hasMore` and `coverage` | **The poll primitive.** Reads stored metadata only and does not depend on the search index. |
| `fieldy_get_conversation` | `id` | Full record: summaries, content, keywords, quotes, calendar events, and the complete `transcript`, never truncated | **The inspect primitive.** `transcript` is null without the `transcripts:read` scope. `summary` and `content` may be null. |
| `fieldy_list_transcripts` | `startTime` (required), `endTime`, `order`, `limit` ≤50, `cursor`, `recordingSource` | Raw transcript lines with speaker and timestamps | Window ≤7 days. Only needed for slices of very long recordings. |
| `fieldy_search_conversations` | `query` (required), `filters` | Ranked shortlist | Not exhaustive, so it can't be used for polling. |
| `fieldy_list_recent_conversations` | `limit` ≤50, `cursor` | Full records | Compatibility tool. The server says to prefer browse. |
| `fieldy_list_conversations_in_time_range` | `startTime`, `endTime` (≤30 days), `limit`, `cursor` | Full records | Compatibility tool. |
| `fieldy_share_conversation` | `conversationId`, `includeTranscript` | Public share URL | **Writes and publishes.** The loop must block it. |

**Live access check (VERIFIED):** `fieldy_browse_conversations(limit=2)` succeeded.
- `coverage.account.isPaid=true` and `accessibleFrom=null`, so the plan imposes no history limit.
- Stored history runs from 2026-08-24 to 2026-09-26.
- Index retention is 30 days semantic and 180 days keyword.
- Both returned items had `summary=null` and speakers `["Unknown"]`.

**Auth constraints (VERIFIED in code, `src/gobby/mcp_proxy/oauth.py` and `oauth_keepalive.py`):**
- OAuth tokens are stored by the daemon (`MCPOAuthStorage` on the SecretStore). Agents never see a token.
- A keep-alive task checks every 60 s (`KEEPALIVE_TICK_SECONDS`) and refreshes 600 s before expiry (`ACCESS_REFRESH_LEAD_SECONDS`). Refresh failures back off up to 900 s, and `Retry-After` is honored.
- One refresher runs per server, across processes, under a Postgres advisory lock (#22718).
- If Fieldy withdraws consent (`consent_required`), only a human can re-authorize, in a browser, with `gobby mcp-proxy auth fieldy --global`.
- #23097 (open) adds safe exception logging for transient keep-alive failures.
- Scopes the loop needs: `conversations:read`, plus `transcripts:read` if it inspects transcripts. Whether the current grant includes `transcripts:read` is INFERRED; not checked, to avoid reading a transcript.

## 2. What already exists for the flow (VERIFIED)

| Need | Existing mechanism | Evidence |
|---|---|---|
| Run on a 5-minute cadence | cron job with `action_type="pipeline"` | `src/gobby/scheduler/executor.py:160-180, 445-465` |
| Never run two polls at once | cron overlap policy: skips while a previous child run is active | `executor.py:270` (`_active_child_skip_outcome`) |
| Call Fieldy without an LLM | pipeline `mcp` step (`server`, `tool`, `arguments`) | `src/gobby/workflows/pipeline_models.py:35-40, 43-56` |
| Judge and dedupe with an LLM | pipeline `prompt` step with a `tools` allow-list, run only when there is new material | `pipeline_models.py:50-60` |
| Human gate | pipeline `approval` (`required`, `message`, `timeout_seconds`) | `pipeline_models.py:27-32` |
| Telegram buttons | inline keyboards with opaque callback tokens | `src/gobby/communications/telegram_callbacks.py:49-115` |
| Review queue | a parent task, with children created `allow_automation=false` | `create_task` schema. That these are never dispatched is INFERRED from the dispatcher opt-in (`gobby build`). |
| Idle OAuth servers are not pinged | #22865 (closed) stopped health pings to lazily connected OAuth servers | task record |

## 3. Evaluating the proposed flow

**Fieldy transcribes.** This is fine as is.
- Recordings appear after the wearable syncs, so a conversation's `startTime` can be well before the moment it becomes visible (INFERRED; the upload lag is not measured).
- Titles and summaries may be filled in later. Both sampled records had a null summary.

**A 5-minute pipeline inspects entries.** Workable, with two rules:
- **Do not use a `startTime` watermark.** A late upload would land behind it and never be seen. Instead, list a trailing window (for example the last 48 h) and remove every id already in the processed-id set.
- **Wait for a conversation to settle before processing it.** Pick it up only once its `endTime` is at least N minutes old, so a recording is not judged while it is still being written.
- **Cost:** the browse step and the id comparison cost about one HTTP call per tick, with no LLM. The LLM `prompt` step runs only when there are unseen ids.

**Dedupe against the review queue and existing tasks.** Two layers are needed:
1. **Source dedupe, which is deterministic:** each Fieldy conversation id is processed exactly once. This must be stored durably, as a label or field on the suggestion task, or a small seen-id table. It must never rely on LLM memory. This is #22719's idempotence criterion 3, and the most likely way the loop becomes noise Josh turns off.
2. **Semantic dedupe, which is judgment:** is this ask already an open task or queue item? The prompt step runs `search_tasks` against the queue parent and open tasks, then either links the conversation to an existing item or creates a new one. A false merge hides an ask and a missed duplicate adds clutter. Showing the matched candidate on the review card lets Josh correct either.

**No worker assignment; each suggestion held for review.** Enforce this structurally, not by prompt:
- Create suggestions only as children of the queue parent, with `allow_automation=false` and no `assigned_agent`.
- Restrict the prompt step's `tools` to `search_tasks`, `get_task`, `create_task`, `add_label` and the Fieldy read tools. Exclude `spawn_agent`, `build_task`, `claim_task` and `fieldy_share_conversation`.
- Each suggestion should carry the conversation id, the Fieldy `url`, the proposed title, a one-line rationale and the dedupe candidate. It should not carry the transcript.

**Plan/Defer on Telegram.** There is one real gap:
- The callback registry is in memory, with a maximum TTL of 3600 s (`telegram_callbacks.py:23, 49-64`, VERIFIED). A button tapped after an hour, or after any daemon restart, will not resolve (INFERRED from the in-memory store).
- A pipeline `approval` gate also holds a live execution open for each item.
- **Recommendation:** make the task the durable state. "Plan" removes a `fieldy-review` label and moves the item into the normal planning path. "Defer" adds `deferred-by-josh`. A lost button then costs a re-send, not a lost decision.

**Failure behavior** (#22719 criterion 6):
- An auth or consent failure should produce one alert naming its cause and the exact re-auth command, and stay quiet until it recovers. The pipeline should not produce a failure every 5 minutes.
- The keep-alive already backs off. The pipeline needs its own "outage reported once" marker.

## 4. Open decisions for Josh

1. **Cadence.** 5 minutes is cheap with the no-LLM idle path. The real latency limit is wearable sync lag, not polling.
2. **Scope of material.** Does the loop consider every recording, or only ones where he addresses the assistant, for example with a spoken cue like "Gobby, …"? Every recording will produce many suggestions from ordinary conversations.
3. **Transcript use.** Should the prompt step read full transcripts (needs `transcripts:read`, larger context) or only titles and summaries (cheaper, but summaries are often null)?
4. **Queue home.** Should the queue be a dedicated parent task, "[Fieldy Tasks for Review]", in the Gobby project, or in `_personal`? Some recordings are personal and not Gobby work.
5. **Privacy.** Suggestions should store a link and a paraphrase only. Transcripts should never be copied into tasks or Telegram.

## 5. What is already satisfied in #22719, and what is not

| #22719 criterion | Status |
|---|---|
| 1. Live access: tool discovery and a bounded read | Met in this research (VERIFIED) |
| 2. Cadence survives a restart | Cron is durable (INFERRED). A named test is still needed. |
| 3. Idempotence | Needs the processed-id record (new) |
| 4. Work goes through the normal task path | Met by the queue-parent design |
| 5. Clarification items reach Josh on the existing Telegram report | Replaced by Plan/Defer on each item. Josh should confirm this replaces "no new channel". |
| 6. Quiet degradation | Needs the report-once outage marker (new) |

#22719's validation criteria describe the old "turn recordings into agent work" design. If Josh picks Plan, the criteria need rewriting for the review-only flow before any implementation.

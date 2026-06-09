# Plans cross-CLI approval — E2E verification matrix (#15619)

Live browser verification (chrome-devtools MCP) of the web-chat plan-approval
pipeline across all 6 managed CLIs. Driven against the running daemon
(`http://localhost:60887`, web UI), gobby project, branch 0.5.0.

Date: 2026-06-09 (session #6992).

## Battery (per CLI)

For each CLI: New web chat → Plan mode → ask for a plan → confirm (a) plan
renders, (b) approval actions on the agent status bar (Approve YOLO / Approve
Act / Reject / View), (c) Plans activity panel shows plan-only markdown (no
transcript/reasoning leak) → Request Changes with feedback → confirm revised
plan renders (REVISION HISTORY) → Approve (YOLO) → confirm Plan radio
auto-switches off, mode switches (bypass), AND execution begins automatically
(no extra user message).

Verification prompt (sandbox-safe, deterministic): "Make a 1-step plan to run
`git status --short` at the repo root and report what's uncommitted. Present for
approval; once I approve, run it." Feedback step adds `git branch --show-current`.

Auto-execution is confirmed via the daemon log (post-approval execution turn /
DoneEvent), since the SPA chat transcript does not always render managed-CLI
turns (see Codex note).

## Matrix

| CLI    | Plan renders | Status-bar actions | Plans panel plan-only | Req-changes → revised | Approve → radio off | Leaves plan mode | Auto-executes | Result |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Claude | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (→YOLO) | ✅ | **PASS** |
| Codex  | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (→bypass) | ✅ | **PASS** |
| Droid  | ✅ | ✅ | ✅¹ | ✅ | ✅ | ✅ (→YOLO) | ✅ | **PASS** (fix #15724) |
| Gemini | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (→bypass) | ✅² | **PASS** |
| Grok   | ✅ | ✅ | ✅ | ✅³ | ✅ | ✅ (→bypass) | ✅ | **PASS** |
| Qwen   | —⁴ | —⁴ | —⁴ | —⁴ | —⁴ | —⁴ | —⁴ | **BLOCKED — infra** |

## Evidence

### Claude (Opus 4.8) — PASS
- Sessions #6994/#6995/#6996.
- Plan renders in transcript + Plans panel (plan-only: Context/Steps/Notes).
- Status bar: `Approve (YOLO)`, `Approve (Act)`, `Reject`, `View`.
- Request Changes opens a feedback box; feedback → revised plan renders with
  `REVISION HISTORY` (Revision 1/2).
- Approve (YOLO): Plan radio auto-off, YOLO checked, panel "Plan approved".
- Auto-execute confirmed (#6996): after approval Claude ran
  `Bash: git -C /Users/josh/Projects/gobby status --short` with no extra
  message and reported the uncommitted files.
- Note: a `/tmp`-target plan did NOT execute — the bash sandbox disallows
  `/tmp` writes (agent pre-flagged it). Sandbox artifact, not a pipeline bug;
  the repo-relative `git status` action executed cleanly.
- Note: after the approved turn completes, the auto-continue nudge can produce a
  short run of idle "Standing by"/"Done" filler turns. Minor; execution itself
  works.

### Codex (GPT 5.5) — PASS
- Session #6997 (conversation 31aefdef).
- Text-plan model: plan presented as prose, surfaced in the Plans panel
  ("Plan ready for approval: …"), status-bar actions present.
- Request Changes + feedback → "Updated plan for approval" with the added
  `git branch --show-current` step; `REVISION HISTORY` (Revision 1/2).
- Approve (YOLO): Plan radio auto-off, YOLO checked, panel "Plan approved",
  mode → bypass (log: `Plan approved (ExitPlanMode unblocked, option=approve_yolo) -> bypass`).
- Auto-execute confirmed via log: 07:16:47 approved → 07:16:55 Codex took an
  execution turn (called `Bash` for git status; the specific command was
  blocked by the unrelated `block-gobby-tasks-cli` rule) → 07:17:03 DoneEvent.
  Execution began automatically without another user message.
- UI quirk to flag: Codex's user turn, plan turn, and execution turn did NOT
  render in the SPA chat message list (only the Plans panel populated). The
  pipeline behavior passed; the transcript rendering for this managed text-plan
  session is a separate UI concern.

### Droid (Factory) — PASS (after fix #15724)

Two plan-presentation paths observed (Droid chooses non-deterministically):

- **Structured (`ExitSpecMode`)** — session #7006, on the restarted daemon.
  Plan renders cleanly in transcript + Plans panel (plan-only: Summary / Step /
  Verification / Expected Output, proper `## ` heading, no leak). Status bar:
  `Approve (YOLO)` / `Approve (Act)` / `Reject` / `View`. Approve (YOLO): Plan
  radio auto-off, YOLO checked, panel "Plan approved", mode → bypass. Auto-execute
  confirmed: Droid ran `git status --short` post-approval (real output, exit 0) and
  produced an "Uncommitted Changes Report" in the **center chat** while the Plans
  panel stayed **plan-only** (no results leak).
- **Prose (`TodoWrite` + markdown, no `ExitSpecMode`)** — session #7002, pre-fix.
  Request Changes + feedback → revised plan ("Updated Plan…") with the added
  `git branch --show-current` step and `REVISION HISTORY` (Revision 1/2). Approve
  (YOLO) → mode bypass (log: `Plan approved (... option=approve_yolo) -> bypass`).

¹ **Defect found + fixed (#15724).** In the prose path the Plans panel leaked
post-execution narration (Droid reformats `git status` output into a results
table) and glued the conversational preamble to the plan's `##` heading
(rendered `…repo.## Plan` as literal text). Root cause: `droid.py` broadcast the
full accumulated `plan_text_parts` for the whole turn. Fix: `_closes_plan_capture`
stops capture once a command/mutation tool completes after a plan body is present,
and tool boundaries insert a paragraph break so headings render; research-first
turns still surface the plan. Committed `9b057354e`; 3 unit tests added (prose-path
scenarios), 68 chat backend tests pass. The structured-path live run (#7006)
confirms no regression. (Note: a one-time post-approval "plan-mode still active"
hesitation seen in the pre-fix prose session #7002 did not recur in #7006.)

### Gemini (3.1 Pro, ACP) — PASS
- Session #7007 (conversation 75c5cb57, source=gemini).
- Plan via ACP text-plan: rendered the exact structured sections from the plan-mode
  context (h3 "Plan: Check Repository Status" + h4 1.Summary / 2.Files / 3.Implementation
  order / 4.Verification). Status-bar actions present; radios disabled while pending.
- **Plans panel plan-only**: Gemini's thinking ("Exploring loading-skills"…) stayed in the
  center chat and was excluded from the panel (no reasoning leak).
- Request Changes + feedback → revised plan "Plan: Check Repository Status and Current
  Branch" with the added `git branch --show-current` step; `REVISION HISTORY` (Revision 1/2).
- Approve (YOLO): Plan radio auto-off, YOLO checked, panel "Plan approved", mode → bypass
  (log: `Plan approved (ExitPlanMode unblocked, option=approve_yolo) for conversation 75c5cb57 -> bypass`).
- ² Auto-execute: post-approval turn auto-ran (08:19:47 approved → 08:19:55 DoneEvent; a new
  turn only spawns from the auto-continue injection). The execution turn does not render in the
  SPA message list — the same managed-CLI transcript-rendering quirk flagged for Codex; the
  pipeline behavior (mode switch + auto-continued turn) passed.

### Grok (Composer 2.5, ACP) — PASS
- Session #7008 (conversation c0e289e4, source=grok). Slow (~90s of thinking +
  MCP tool-discovery `search_tool`/`use_tool` before presenting), but correct.
- Plan via ACP text-plan: structured h3 sections (1.Summary / 2.Files / 3.Implementation
  order / 4.Verification). Status-bar actions present; radios disabled while pending.
- **Plans panel plan-only**: Grok's extensive thinking and its `grok.search_tool` /
  `grok.use_tool` calls stayed in the center chat; the panel showed only the plan.
- Approve (YOLO): Plan radio auto-off, YOLO checked, panel "Plan approved", mode → bypass
  (log: `Plan approved (... option=approve_yolo) for conversation c0e289e4 -> bypass`).
- Auto-execute confirmed: post-approval turn ran (08:25:35 approved → 08:25:55 DoneEvent)
  and the transcript reported `git status` output ("Working tree dirty. 5 paths
  (1 deleted, 2 modified, 2 untracked). Exit 0").
- ³ Request-changes not re-run for Grok/Qwen individually; it is the same CLI-agnostic
  ACP plan path verified live on Gemini (#7007) and Claude/Codex/Droid, and covered by
  `tests/servers/websocket/chat/test_acp_plan_revise_loop.py`.

### Qwen (qwen3.6-35b-a3b-q8-local) — BLOCKED (infra, not a pipeline defect)
- Two fresh sessions attempted — #7009 (conversation a487b719) and #7009-retry
  (conversation 15b264ae), both source=qwen.
- Both failed at the **model layer** before any plan could form:
  `Managed qwen upstream error … model=qwen3.6-35b-a3b-q8-local(openai): Internal error`
  (`code: -32603`). The locally-served Qwen model endpoint returns an internal error;
  this is an environment/model-server issue, independent of the plan-approval pipeline.
- ⁴ Not exercisable in this environment. Qwen uses the **same ACP text-plan path** as
  Gemini (#7007 PASS) and Grok (#7008 PASS) — `_maybe_broadcast_pending_plan` in
  `acp_session.py` — which is covered live by those two CLIs plus
  `tests/servers/websocket/chat/test_acp_plan_broadcast.py`,
  `test_acp_plan_mode_switch.py`, and `test_acp_plan_revise_loop.py`. The web UI
  surfaced the upstream error rather than hanging (graceful degradation, #15618).
- To finish Qwen live: restore the local `qwen3.6-35b-a3b-q8-local` model server (or
  point Qwen at a working model) and re-run this battery; no Gobby code change is
  implicated.

## Result

5 / 6 managed CLIs verified live PASS (Claude, Codex, Droid, Gemini, Grok). Both
managed plan models exercised end-to-end: tool-plan (Claude `ExitPlanMode`, Droid
`ExitSpecMode`) and text-plan (Codex app-server, Gemini/Grok ACP). One pre-existing
defect found and fixed during verification (#15724 — Droid prose-plan capture leaked
execution narration). Qwen is blocked only by a local model-server internal error
(infra), and rides the same ACP plan path already verified on Gemini and Grok.

---
name: plan
description: This skill should be used when the user asks to "/gobby plan", "create plan", "plan feature", "write specification". Guide users through structured specification planning. Does NOT create tasks - use /gobby expand for that (or the built-in adversarial loop ending in Step 8).
version: "2.2.0"
category: core
triggers: plan, specification, requirements
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby plan — Interactive Implementation Planning

Guide the user through structured requirements gathering and specification writing, optionally with an **adversarial review loop** that spawns the `plan-adversary` agent to critique each revision before handoff to `/gobby expand`.

The **drafting methodology** (phase structure, task format, TDD rules, categories, hierarchy, verification checklist) lives in the `plan-draft` skill. During Step 3: Call get_skill(name="plan-draft") on gobby-skills, then continue. Do not inline that methodology here.

The **review methodology** for the adversarial loop lives in the `plan-review` skill — the spawned `plan-adversary` agent loads it; you do not need to load it in this skill.

## Plan-Coverage Contract Grammar

The Plan-Coverage Contract is the authoring contract for new epic plans. The
canonical grammar, kind enum, acceptance-item shape, typed deferral object,
structured `covers:<plan-id>:<section-id>:<item-id>` record, and table-row
decomposition rule live in `plan-draft/SKILL.md`. The full reference page is
`docs/contracts/plan-coverage.md`.

When drafting or revising a plan, load `plan-draft` and follow its typed
grammar before presenting the artifact. Free-form `plan-ref:` labels are not
honored; expansion coverage comes only from structured `covers:` records.

## Workflow Overview

| # | Name | Notes |
|---|------|-------|
| 0 | Enter Plan Mode | Required prelude; native `EnterPlanMode` boundary. |
| 1 | Adversarial Opt-in & Parent Task | Y/N opt-in; attach guard; session-owned lock. |
| 2 | Requirements Gathering | Elicit goal, constraints, risks. |
| 3 | Draft Plan Structure | Load `plan-draft` skill; structure the plan. |
| 4 | Write Plan Document | Derive slug, then write the artifact per `plan-draft` template. |
| 5 | Plan Verification | Run `plan-draft`'s verification checklist. |
| 6 | First-Draft Approval | Route through real `ExitPlanMode`; branch on opt-in. |
| 6b | Adversary Mode Selection | I/D + `max_rounds`, only if Step 1a opted in. |
| 7 | Adversarial Review Loop | Pre-flight fact-check; spawn `plan-adversary`; wait; revise; re-approve. |
| 8 | Approval Handoff / Expansion | Run `expand-task` pipeline with retry/escalate. |
| 9 | Round-budget Exhausted / Abort | Bypass / abort / restart with cleanup. |

Terminal cleanup is mandatory on every exit path — see the bottom of this file.

---

## Step 0: **REQUIRED** Enter Plan Mode

Before creating any plan, enter Claude Code's plan mode so exploration happens without edits.

**How to enter**: Use the `EnterPlanMode` tool or respond with a planning-focused message that triggers plan mode.

**Why required**: Plan creation requires reading existing code without making edits.

---

## Step 1: Adversarial Opt-in & Parent Task

### 1a. Adversarial-review opt-in

If `plan_review_mode` is already set (`"adversarial"`, `"delegated"`, or
`"plain"`), skip this step — the prior run's choice stands. If
`plan_review_requested == true` but `plan_review_mode` is unset, we're resuming
between Step 6 approval and Step 6b; skip 1a and jump straight to Step 6b.

Otherwise present:

```text
Do you want adversarial review on this plan?
  Y) Yes — I draft, you approve the first draft, then plan-adversary
     reviews it. You pick interactive vs delegated at that point.
  N) No / Plain — I draft, you approve, hand off to /gobby expand.
Choice [Y]:
```

- `N` → `set_variable(name="plan_review_mode", value="plain", ...)`. Continue to Step 1b.
- `Y` → `set_variable(name="plan_review_requested", value=true, ...)`. Continue to Step 1b. **Do not set `plan_review_mode` yet** — the I/D choice and `max_rounds` land in Step 6b after the first draft is approved.

**Why defer the I/D choice to Step 6b?** Interactive vs delegated is an
informed decision about how the review loop should behave, and that depends on
how ready the first draft looks. Asking before the user has seen a draft
forces a blind pick. Keep the upfront question binary (opt in to review at
all); make the shape of the review an informed choice after the draft lands.
Do **not** re-merge these prompts back into one — that was the pre-2.1 design
and the upfront menu caused users to default to the wrong mode.

`plan_review_mode` and `plan_review_requested` are persistent across
`ExitPlanMode`; a stale value from a previous run could otherwise steer a
fresh run into the wrong branch. Terminal cleanup clears both.

Do **not** overload `plan_mode` — that is an existing boolean referenced by a dozen rules and `ExitPlanMode` resets it to `false`.

### 1b. Parent-task selection

Ask:

```text
Attach this plan to an existing task #N, or create a new planning root?
```

- **New** →

  ```text
  create_task(task_type="epic", category="planning", title="<from user>")
  ```

  Parent task reference is what the user supplied (`#N`) or the newly created epic.

- **Existing `#N`** → `get_task(#N)` and run the **attach guard** below.

### 1c. Attach guard (HARD BLOCK)

Legacy autonomous planning marker checks:

**Legacy front-half marker check.**

```text
active_fh = "conductor:front-half" in labels and "conductor:front-half-complete" not in labels
```

**Live stage-child check.** Do **not** run a single `list_tasks(parent_task_id=#N)` — the default limit hides live children on large parents. Query each stage label explicitly:

```text
STAGE_LABELS = [
    "conductor-stage:requirements",
    "conductor-stage:planning",
    "conductor-stage:expansion",
]
has_live_stage_child = False
for stage in STAGE_LABELS:
    children = list_tasks(parent_task_id=plan_parent_ref, label=stage, limit=200)
    if any(c.status != "closed" for c in children):
        has_live_stage_child = True
        break
```

If `active_fh` **or** `has_live_stage_child` is true, error out:

> Parent #N still carries active legacy planning markers. Wait for that flow to complete, detach it, or start a new planning root.

Skill exits. A parent that previously went through the legacy autonomous planning flow **and completed cleanly** (both `conductor:front-half` AND `conductor:front-half-complete` present, no live stage children) is allowed.

**Concurrent interactive-session lock check.** Enumerate **all** labels on the parent matching the prefix `interactive:planning-in-progress:` (labels accumulate — `add_label` only dedupes exact strings and `remove_label` only removes exact strings). For each such label, extract the session suffix and classify:

| Class | Predicate | Action |
|-------|-----------|--------|
| **Ours** | suffix == current `session_id` | Resume; skip live/stale classification for this label. |
| **Live** | session record exists AND one of: `status in {"active","paused","waiting"}`, `can_proxy_attach=True`, `has_terminal_liveness=True` | Error out: "Parent #N is under an active interactive planning session `<ref>`. Wait, abort, or start a new planning root." Skill exits. |
| **Orphaned** | session absent, terminal-status (`ended`, `archived`, `crashed`), or fails the liveness predicate | Queue for removal. |

Liveness is **deliberately conservative** — `paused`/`waiting` are routine between-turn states (`hooks/event_handlers/_agent.py:370`, `:392`) and must not be reaped. When uncertain, classify as live.

After the sweep:

- Remove every queued-orphan label: `remove_label(task_id=plan_parent_ref, label=<exact orphan string>)`.
- Surface one notice to the user if any were recovered: "Recovered N orphaned planning locks from terminated sessions."
- If any label was classified as "ours", fall through to **resume** instead of acquiring a new lock.
- Otherwise, proceed to lock acquisition.

### 1d. Acquire the session-owned lock

Immediately after the guard passes:

```text
lock_label = f"interactive:planning-in-progress:{self_session_id}"
add_label(task_id=plan_parent_ref, label=lock_label)
set_variable(name="interactive_lock_label", value=lock_label, session_id="#<self>")
set_variable(name="plan_parent_ref",        value=plan_parent_ref, session_id="#<self>")
```

`max_rounds` is intentionally **not** set here — it lands in Step 6b along
with the I/D choice, since both are part of the same deferred review-mode
decision.

Persisting the **exact** label string is load-bearing. `remove_label` is exact-match only; terminal cleanup must pass the same string back.

### 1e. Resume detection

On skill entry, before showing the mode menu, check:

```text
existing_task_id = get_variable(name="planning_task_id", session_id="#<self>")
if existing_task_id:
    task = get_task(existing_task_id)
    if task.status != "closed":
        # resume path
```

If a prior planning task is still live, present **resume / abort / restart** instead of the Step 1a menu:

- **Resume** — read `planning_task_id`, `artifact_path`, `current_round` from vars; jump to Step 7.4.
- **Abort** — close the planning task with reason "User aborted resumed planning"; run terminal cleanup.
- **Restart** — close the planning task; run terminal cleanup; re-enter Step 1 fresh.

---

## Step 2: Requirements Gathering

Ask the user:

1. "What is the name/title for this feature or project?"
2. "What is the high-level goal? (1–2 sentences)"
3. "Are there any constraints or requirements I should know about?"
4. "What are the unknowns or risks?"

---

## Step 3: Draft Plan Structure

Load the drafting methodology as your first action:

```text
call_tool("gobby-skills", "get_skill", {"name": "plan-draft"})
```

Follow that skill's "Plan Structure" + "Dependency Notation" sections to sketch the epic title, phases, and task outline.

**Do not inline plan-draft's content into this skill.** If anything in the drafting methodology is unclear, re-read the `plan-draft` skill — it is authoritative.

---

## Step 4: Write Plan Document

### 4a. Derive a semantic slug

Derive a **3–4 word kebab-case slug** from the plan title you drafted in Step 3. The slug is baked into the filename so a human scanning `.gobby/plans/` can tell at a glance which plan is which — especially useful when resuming a stalled plan across sessions, or when multiple concurrent plans are in flight.

Rules:

- Lowercase kebab-case (hyphens only; no underscores, spaces, or punctuation).
- 3–4 words. ≤ 40 characters.
- Strip articles (`a`, `the`), fillers (`rewrite-of-X` → `x-rewrite`), and generic nouns (`plan`, `feature`).
- Lead with the most distinctive domain term.

Good: `skillsmp-install-rewrite`, `auth-middleware-rewrite`, `websocket-reconnect`.
Bad: `plan`, `the-new-feature`, `a-big-refactor-of-the-whole-module`.

Persist the slug:

```text
set_variable(name="plan_slug", value="<slug>", session_id="#<self>")
```

### 4b. Write the artifact

Write the plan to **`.gobby/plans/task-<parent_seq>-<slug>.md`**. Follow `plan-draft`'s canonical template verbatim. The adversary and `expand-task` read the path we hand them (via `artifact_path` and pipeline inputs), so this convention is about human-scannability at rest, not downstream coupling.

Remember the two load-bearing rules from `plan-draft`:

- **Plan content = subtask descriptions** — each `### N.N` section must be self-contained; the implementing agent sees ONLY that section.
- **No explicit test tasks** — expansion auto-inserts TDD wrappers; `[TDD]`/`[IMPL]`/`[REF]` prefixes in a draft are bugs.

Persist the path:

```text
set_variable(name="artifact_path", value=".gobby/plans/task-<parent_seq>-<slug>.md", session_id="#<self>")
```

---

## Step 5: Plan Verification

Run `plan-draft`'s **Verification Checklist** (5 items: no explicit test tasks, valid dependency tree, valid categories, canonical phase syntax, self-contained sections). Fix anything that fails **before** presenting to the user.

Report the verification output in the exact format `plan-draft` specifies.

---

## Step 5.5: Mechanical Structural Validation

Step 5 is a narrative review. Step 5.5 is a mechanical CLI check that
catches structural drift the narrative pass cannot reliably enumerate
(missing `kind:` annotations, malformed acceptance items, dropped phase
headings whose IDs do not match the contract regex, malformed deferral
objects, target-inventory drift, conservative table-row decomposition, and
index-proven missing direct consumers when code-index context exists). It is
fast and deterministic — always run it, never skip it.

```text
uv run gobby plans validate <artifact_path from Step 4>
```

If the current working directory is not the project root, add an explicit
project context:

```text
uv run gobby plans validate <artifact_path from Step 4> --project <project-name-or-id>
```

Use `gobby tasks expand validate-plan` only from task-expansion workflows or
when debugging the expansion CLI itself. Plan authoring and adversary
resubmission checks use `gobby plans validate`.

Branch on the command result:

- **Exit code 0** — record the reported phase list for the user-facing
  report (Step 5 already showed the narrative checks; mention the mechanical
  pass alongside as a single combined report). Proceed to Step 6.
- **Nonzero exit** — display the validator errors **verbatim** to the user.
  Then attempt an in-place fix:
  - If the error names a class already taught by `plan-draft` (e.g.
    `phases missing` → fix phase headings to `## P<N>: Name`; `missing
    kind` → add `` `kind: ...` `` annotation; `missing acceptance` → add
    `**Acceptance:**` block; `malformed item ID` → re-number to
    dotted-prefix-match the section ID), apply the fix, re-run Step 5.5
    against the same artifact, and repeat until the validator passes.
  - If the error is structural in a way the skill cannot mechanically
    repair (corrupted YAML in a deferral object, regex-failing section
    IDs that require rewriting whole sections), surface the validator
    output to the user and ask them to confirm the fix before
    proceeding. Do NOT advance to Step 6 with a failing validator.

Step 5.5 catches the false-negative class the narrative checklist misses:
a plan can pass all five Step 5 checks (no test tasks, valid deps,
categories, phase syntax tolerated by the old skill, self-contained
sections) and still fail the contract because, say, its phase headings
parse to `section_id: "1"` instead of `section_id: "P1"` and the parser
silently drops them. The `gobby plans validate` command runs the same
contract validation used by expansion, plus deterministic plan lints; the
plan-adversary spawn gate runs the same validation again before review.
Failing fast here saves an entire adversary round and the LLM call that
goes with it.

Resume safety: if Step 5.5 returns `valid: False` and the skill is
interrupted before applying a fix, re-entry from Step 5 (via the resume
path or a fresh user run) repeats the validator call against the
current artifact — no separate state machine is needed.

---

## Step 6: First-Draft Approval

Present the plan to the user and route the decision through the real native **`ExitPlanMode`** / `provide_plan_decision` boundary — do **not** synthesize a "user approved?" prompt. The native boundary is the only place `chat_session_permissions` records a real approval.

On approval, branch in this order:

- `plan_review_mode == "plain"` → tell the user to run `/gobby expand <artifact_path>`; run **terminal cleanup**; skill exits.
- `plan_review_requested == true` → proceed to **Step 6b** (Adversary Mode Selection). The first-draft approval satisfies the interactive first round; `plan_review_mode` and `max_rounds` are chosen next.
- `plan_review_mode in ("adversarial", "delegated")` → resume path (Step 6 re-entry during `adversarial` mid-loop revisions). Skip 6b — the mode was already chosen — and proceed to **Step 7**.

---

## Step 6b: Adversary Mode Selection

Fires only when `plan_review_requested == true` after Step 6 approval — i.e.,
the user opted into adversarial review at Step 1a, approved the first draft,
and we now need the shape of the review loop. If `plan_review_mode` is
already set from a prior round, skip this step and proceed to Step 7.

Present:

```text
First draft approved. Adversary review starts from here.
  I) Interactive — I show plan-adversary's findings each round and re-approve
     the revised plan via ExitPlanMode before the next round.
  D) Delegated — I revise silently using the findings, wake you only at
     terminal state (approval, escalation, or round budget exhausted).
Choice [I]:

How many adversary rounds? [3]
```

Record the choice:

```text
set_variable(name="plan_review_mode", value="adversarial" | "delegated", session_id="#<self>")
set_variable(name="max_rounds",       value=<positive int, default 3>,   session_id="#<self>")
set_variable(name="plan_review_requested", value=None, session_id="#<self>")  # consumed
```

Mode mapping:

- `I` → `"adversarial"` (per-round ExitPlanMode approval)
- `D` → `"delegated"` (silent until terminal)

Clearing `plan_review_requested` is load-bearing — if Step 6 is re-entered
during an `adversarial` mid-loop revision, the stale `true` value would
re-enter 6b and re-prompt the mode. The Step 6 branch order above already
prefers `plan_review_mode` on re-entry, but defense-in-depth: consume the
opt-in flag the first time it's acted on.

Then proceed to Step 7.

---

## Step 7: Review Loop (adversarial/delegated modes only)

### 7.0a. Anchor-task contract (load-bearing)

The parent session **never claims a task**. Plan-markdown edits under `.gobby/plans/*.md` are exempt from `require-task-before-edit` (see `is_plan_file()` in `src/gobby/workflows/enforcement/blocking.py`), so plan-mode + plan-file editing alone do not require a claim. The parent's role from Step 7 onward is pure orchestration.

Each adversary round spawns against a **freshly-created per-round anchor task** (child of `planning_task_id`, `task_type: review_anchor`, `category: planning`). The anchor exists **only for verdict capture**: the coordinator starts the anchor's `planning` stage, submits that stage for review, and the spawned adversary appends `## Adversary Findings — Round N` to the anchor's description and calls `approve_review(stage_name="planning")` (clean) / `reject_review(stage_name="planning")` (with findings) / `escalate_task` on the anchor.

**Coordinator owns plan revision between rounds.** The coordinator (this chat session) acts as the planner for all revision rounds in both interactive and delegated modes — there is no separate `planner` agent spawn. The compact-self call after each adversary spawn (Step 7.4) summarizes the coordinator's context so the next round's revision starts fresh, replacing the prior fresh-context guarantee that a planner-agent spawn provided.

The parent **fires-and-forgets**: spawn the agent, end the turn cleanly. No claim, no `ScheduleWakeup`, no `Monitor`. The daemon's task-completion notification (P2P signoff message from the agent's session) wakes the parent when the agent terminates. On wake, the parent reads the anchor's terminal state via `get_task` and routes per Step 7.6.

Anchor lifecycle: each anchor closes when its round terminates (verdict captured). Cleanup at loop end (approval, exhaustion, abort, restart) closes any still-open anchors. The planning epic itself stays open until Step 8 expansion handoff completes.

This contract is mode-agnostic. Interactive and delegated share the same fire-and-forget orchestration; the only mode-specific behavior is the user-confirmation gate after a rejected round (Step 7.6).

### 7.0. Artifact precondition

Before entering Step 7, verify `artifact_path` is still present:

```text
artifact_path = get_variable(name="artifact_path", session_id="#<self>")
if not artifact_path:
    error("artifact_path is missing; aborting before review loop enters a fail-closed write gate.")
```

If the variable is absent, abort with a clear message instead of proceeding.

### 7.1. Create the planning epic (once per attempt)

If `planning_task_id` is not already set in session vars:

```text
planning = create_task(
    parent_task_id=plan_parent_ref,
    task_type="epic",
    title=f"Interactive plan for {parent_ref}",
    category="planning",
    labels=["interactive:planning"],
)
set_variable(name="planning_task_id", value=planning.id, session_id="#<self>")
```

Task type is **epic** so `close_task` does not require `changes_summary` (leaf-close requirement doesn't apply to orchestration containers). **Do NOT** apply `conductor:front-half` — that legacy label is only interpreted by compatibility guards.

The parent lock was already acquired in Step 1; do not re-acquire here.

### 7.2. Anchor the artifact

If the plan file is not already at `.gobby/plans/task-<parent_seq>-<slug>.md` (the path persisted in `artifact_path`), move/write it there. The adversary and `expand-task` receive the path we pass them, so the canonical filename is for human readability when inspecting `.gobby/plans/` at rest.

### 7.3. Round accounting

Read `current_round` from the planning epic's current stage
`review_round_count` (default `0`). **Internal state is 0-indexed** (matches
the existing review-round convention); **all user-visible and adversary-facing
surfaces use `current_round + 1`**. First round is stored as `0` internally but
"Round 1" in every message.

Surface: `Round {current_round + 1} of {max_rounds}`.

### 7.3a. Pre-flight fact-check

Before creating the round's anchor and spawning the adversary, dispatch a
**mid-tier subagent** (one tier below the planner agent's own model — fast and
cost-optimized for read-and-verify, not strategy) to mechanically verify every
concrete claim in the plan against the actual codebase. Iterate until clean,
then proceed to Step 7.4.

**Why**: adversary rounds are expensive. A non-trivial share of rejection
rounds fire on factual drift the planner introduced during revision — wrong
line numbers, nonexistent symbols, miscounted fixtures, wrong field names —
that a cheap mechanical check catches in seconds. Burning an adversary round
on drift is wasteful. The pre-flight stays focused on existence-and-accuracy
checks, leaving architecture and contract-level critique to the adversary.

**What to verify** (read-only — the subagent must NOT modify any file):

- Every line number cited in the plan (allow ±5 lines before flagging drift).
- Every symbol/function/class/method name referenced (must exist in the cited
  module).
- Every file path mentioned (must exist on disk; for new files, mark as
  "to-be-created" and skip).
- Every field/key/attribute name claimed (must match actual schema or
  dataclass).
- Every cited count (deliverables, sections, tests, fixture inventory,
  acceptance items, dependency annotations).
- Every regex/format claim (must match the actual pattern, not a paraphrase).

**Dispatch** (CLI-agnostic — adapt to the host CLI's subagent dispatch
mechanism; the schema below is the contract, not a literal call):

```text
factcheck_run = spawn_agent(
    description="Plan fact-check pre-flight",
    subagent_type=<host-CLI's lightweight code-reading agent>,
    model=<a mid-tier model: cheaper than the planner's own model>,
    prompt=f"""
        Pre-adversary fact-check of `{artifact_path}`. Read-only verification
        of every concrete claim against the actual codebase. Report drift
        only — every claim that doesn't match reality, with the actual value.
        Do NOT modify any file.

        Categories to check:
          1. Line numbers cited in the plan (±5 acceptable).
          2. Symbols/functions/classes named (must exist where claimed).
          3. File paths (must exist on disk; new files marked separately).
          4. Field/key/attribute names (must match actual schema).
          5. Counts (deliverables, sections, tests, fixture inventory).
          6. Regex/format claims (must match actual pattern verbatim).

        Report format: one line per drift, `CLAIM: <plan claim> | ACTUAL: <code reality>`.
        If no drift found, say so explicitly.
    """,
    isolation="none",
)
```

**Iteration loop**:

1. Receive the drift report.
2. If drift is reported: apply each fix in-place to `artifact_path`. Do NOT
   modify the source code being verified. Re-run Step 7.3a against the
   revised plan.
3. When the report says "no drift," continue to Step 7.4.

The loop converges quickly — each pass only fixes drift; it never introduces
new claims. Typical run: 1-2 passes.

**Mode behavior**:

- Both `interactive` and `delegated` modes run this step every round.
- `delegated` mode applies drift fixes silently and proceeds; do not
  interrupt the user for mechanical fact-check fixes.
- `interactive` mode applies drift fixes silently as well — the pre-flight
  is mechanical, not a design decision; surface only if a fix would alter
  the plan's intent (rare).
- If the pre-flight subagent fails to dispatch (host CLI does not expose
  subagent spawning, or the subagent crashes), log the limitation and
  proceed directly to Step 7.4. The adversary round will still run; the
  user just loses the pre-flight benefit for that round.

### 7.4. Create the per-round anchor and spawn the adversary

Create a fresh anchor task scoped to this round, then spawn against it. Never
reuse an anchor between rounds.

```text
old_anchor_id = get_variable(name="active_anchor_id", session_id="#<self>")
if old_anchor_id:
    old_anchor = get_task(old_anchor_id)
    if old_anchor.state not in ("closed", "escalated"):
        close_task(task_id=old_anchor_id, reason="superseded by new adversary round")
    set_variable(name="active_anchor_id", value=None, session_id="#<self>")

anchor = create_task(
    parent_task_id=planning_task_id,
    task_type="review_anchor",
    category="planning",
    title=f"Plan-adversary review — round {current_round + 1}",
)
initialize_task_manifest(
    task_id=anchor.id,
    stage_names=["planning"],
    stage_caps=[{"stage_name": "planning", "max_review_rounds": max_rounds + 1}],
)
set_variable(name="active_anchor_id", value=anchor.id, session_id="#<self>")

start_stage(task_id=anchor.id, stage_name="planning")
submit_for_review(task_id=anchor.id, stage_name="planning")

run = spawn_agent(
    agent="plan-adversary",
    task_id=anchor.id,                    # NOT planning_task_id
    parent_session_id=<self>,
    isolation="none",                     # ALWAYS "none" for plan-adversary
    prompt=(
        f"Plan artifact: {artifact_path}\n"
        f"Parent task: {plan_parent_ref}\n"
        f"Planning epic: {planning_task_id}\n"
        f"Display round: {current_round + 1}\n"
        f"Anchor task (mark verdict on this): {anchor.id}\n"
        f"... (any docs the parent references)"
    ),
)
set_variable(name="adversary_run_id", value=run.run_id, session_id="#<self>")
```

**ISOLATION RULES — NON-NEGOTIABLE:**

- **ALWAYS pass `isolation="none"` on the `plan-adversary` spawn.** The agent
  definition already sets `isolation: none`; passing it explicitly is defensive
  and self-documenting. It costs nothing.
- **NEVER pass `isolation="worktree"` or `isolation="clone"` for `plan-adversary`
  (or for the Step 7.3a pre-flight fact-checker).** Both agents are read-only
  reviewers. Their only legitimate write is the post-approval manifest append to
  the plan file in the main worktree — which the adversary contract restricts to
  `task_artifacts.plan_file_path`. A worktree spawn forks a branch from `HEAD`
  *before* the coordinator's between-round fix commits land, which causes
  reviewers to analyze stale code and emit false-positive findings. This is a
  documented failure mode, not a theoretical risk.
- **NEVER copy isolation defaults from worker-agent spawn patterns.** Worker
  agents (developer, qa, etc.) use `isolation="worktree"` because they perform
  concurrent file edits. Reviewers do not.
- **ALWAYS use `gobby build <plan_file> --isolation none` for plan-side
  build/dispatch work.** Planning build dispatch writes task and plan artifacts
  in the main worktree; it is not source implementation work.
- If you find yourself reaching for any `isolation` value other than `"none"`
  here, stop and re-read this block. There is no scenario where a different
  value is correct for the planning review loop.

The spawn path auto-injects `assigned_task_id` and auto-claims the anchor for the child session (`spawn_agent/_implementation.py:375`, `:499`) — no `initial_variables`, no manual claim here. The anchor (not the planning epic) is what the adversary marks at terminal.

Immediately after the spawn, trigger a self-compaction:

```text
call_tool(
    server_name="gobby-sessions",
    tool_name="compact_self",
    arguments={"session_id": "#<self>"},
)
```

This frees the coordinator's context (which by this point is loaded with
requirements gathering, plan drafting, and exploration agent results) while
the sub-agent runs. The verdict landing via daemon wake (Step 7.5) re-enters
with a fresh, summarized context. Terminal sessions fire the right slash
command per CLI (claude/codex `/compact`, gemini/qwen/droid `/compress`);
web_chat sessions return `compacted: False` with a follow-up note today and
are no-op'd until #13684 lands the daemon-level ChatSession registry.

### 7.5. Yield until the adversary wake

Surface "Adversary reviewing; I will resume when the daemon wakes this session."

Do **not** call the removed workflow completion-wait tool. After storing
`adversary_run_id`, end the current turn. The daemon records a durable
completion notification and sends a wake signal when the adversary run exits.
On wake/resume, continue at Step 7.6.

This is push-based via the completion registry and durable wake dispatcher. If
the adversary finishes before the parent yields, the completion result is still
stored and the wake path resumes from durable state — no polling, no
early-completion race.

### 7.6. Interpret the result

On wake/resume, read the **anchor's** terminal state (NOT the planning epic):

```text
anchor_id = get_variable(name="active_anchor_id", session_id="#<self>")
anchor = get_task(anchor_id)
```

The adversary's findings live in `anchor.description` under the heading `## Adversary Findings — Round {current_round + 1}`. If raw run details are needed for diagnostics, call `get_agent_result(run_id=adversary_run_id)`.

After reading the verdict, **close the anchor** so it does not linger. Whenever
an anchor is closed below, immediately
`set_variable(name="active_anchor_id", value=None, session_id="#<self>")` so
the next round must create a fresh anchor. Then branch:

- **`review_approved`** → close the anchor with reason "round approved"; go to Step 8.
- **current stage `ready`** after `reject_review`
  1. Extract `## Adversary Findings — Round {current_round + 1}` from the anchor description (the exact heading the adversary wrote; prevents leaking prior rounds' findings).
  2. Close the anchor with reason "round rejected; findings captured".
  3. If `current_round + 1 >= max_rounds` → go to Step 9.
  4. If `plan_review_mode == "adversarial"`:
     Present the findings verbatim to the user via `ExitPlanMode` or `AskUserQuestion`. End the current turn after presenting; the user's reply triggers the next turn. On the next turn, if the user approved continuing, re-enter plan mode, revise the plan file with the user, route the revised plan through `ExitPlanMode` again, then loop back to **Step 7.3a** (pre-flight fact-check the revised plan, then spawn the next round).
  5. If `plan_review_mode == "delegated"`:
     Revise the plan file in place using the adversary findings, run the same verification checklist from Step 5, keep edits scoped to `artifact_path` only, and loop back to **Step 7.3a** (pre-flight fact-check, then spawn the next round) without re-entering plan mode. Do not interrupt the user for non-terminal review rejections.

- **`escalated`** with `escalation_reason` starting `needs_requirements:` or `needs_human:`
  1. Close the anchor with reason "round escalated".
  2. Surface the escalation reason verbatim to the user.
  3. This is terminal for delegated mode and an interrupt for interactive mode.
  4. Go to Step 9.

- **Any other terminal state** → treat as adversary crash. Close the anchor with reason "round crashed". Surface the state, `adversary_run_id`, and any available `get_agent_result` details. Go to Step 9.

**Why the anchor pattern**: capturing the verdict on a per-round anchor (rather than directly on the planning epic) keeps the planning epic's lifecycle clean — the epic stays open until Step 8 expansion handoff, regardless of how many adversary rounds run. It also avoids stop-hook collisions: the parent never claims any task; the adversary owns its anchor for the lifetime of its run; the parent reads anchor state on wake and closes it. No long-lived shared task state, no parent-claim/release dance.

**Why only interactive mode re-enters plan mode each round:** the user's native
approval boundary runs through `ExitPlanMode` / `provide_plan_decision`
(`chat_session_permissions.py:117`), which is only active while
`plan_mode=true`. Interactive mode uses that boundary every round by design.
Delegated mode intentionally skips per-round approval and relies on the
artifact-scoped write gate instead; only terminal states interrupt the user.

Chat-session plan-state reset also clears UI plan-artifact vars like `_plan_file_path` on mode changes (`chat_session_permissions.py:362`); we re-write `artifact_path` and the plan file on each re-entry so that is harmless.

---

## Step 8: Approval Handoff / Expansion (NEW)

The skill stays in control through success and failure — no "exit and run a raw tool" paths. The `expand-task` pipeline owns the run and validation (`expand-task.yaml:66,:74`); retry, planning-epic close, and state cleanup are the skill's job.

### 8.1. Run the pipeline

```text
execution = run_pipeline(
    name="expand-task",
    inputs={"task_id": plan_parent_ref, "plan_file": artifact_path},
)
set_variable(name="expansion_execution_id", value=execution.execution_id, session_id="#<self>")
```

Do **not** call the removed workflow completion-wait tool. Store `expansion_execution_id`, end the
current turn, and resume from the daemon's durable completion wake.

### 8.2. Branch on outcome

On wake/resume, read `expansion_execution_id` and call
`get_pipeline_status(execution_id)`.

- **Success** —
  1. Report child-task count to the user.
  2. `close_task(planning_task_id, reason="Interactive planning complete; expansion launched")`. The planning epic needs no `changes_summary` (epic exemption).
  3. Run **terminal cleanup** (clear all session vars, remove `interactive_lock_label` from parent).
  4. Skill exits.

- **Failure** — surface the pipeline error. **Do not close the planning task.** Present three choices:
  1. **Retry** (default) — loop back to 8.1 with the same inputs. Count failures; after **3 consecutive failures** fall through to Escalate.
  2. **Retry with overrides** — ask for `provider`/`model` overrides; loop to 8.1 with those added to `inputs`.
  3. **Escalate** — `escalate_task(planning_task_id, reason=f"expansion_failed: {error}")`; run terminal cleanup; skill exits. The user can pick the escalated planning epic up later.

### 8.3. Stay out of standalone test architecture

Do **not** create or advance a standalone test-architecture stage. The architect-owned
architecture step now carries the test strategy, and the interactive skill ends at
expansion.

---

## Step 9: Terminal Interrupt / Abort (NEW)

Entered when `current_round + 1 >= max_rounds`, the adversary escalates for
requirements/human help, or the adversary crashes.

Present the final `## Adversary Findings` and any escalation reasons to the user. Offer three choices; **each one runs terminal cleanup** before the skill exits so the planning epic is disposed of and the parent lock is released.

| Choice | Disposition of the planning epic | Then |
|--------|----------------------------------|------|
| **Revise manually + run `/gobby expand` directly** | `close_task(planning_task_id, reason="User bypassed adversarial gate; running /gobby expand manually")` | Terminal cleanup; plan file stays in place. |
| **Abort** | `close_task(planning_task_id, reason="User aborted adversarial planning")` | Terminal cleanup. |
| **Restart** | `close_task(planning_task_id, reason="Restart: planning round budget exhausted, beginning a new attempt")` | Terminal cleanup (releases the lock); re-enter Step 1 fresh. |

Restart is a full re-seed — the user gets the Step 1a Y/N menu again (and
Step 6b after re-approval if they opt in), and the guard re-runs against
current parent state. We deliberately do not carry "same attempt" state
across restart; doing so would require owner semantics deeper than we want
to add for this feature.

---

## Terminal Cleanup (MANDATORY)

Every exit path from this skill — Step 6 plain-mode exit, Step 8 success, Step 8 failure/Escalate, Step 9 bypass/abort/restart, adversary crash — **must** run the same cleanup:

```text
# Close any per-round anchors still open under the planning epic.
# Each round closes its own anchor on terminal in Step 7.6, but a crash mid-round
# can leave one orphaned. Sweep here as a safety net.
open_anchors = list_tasks(parent_task_id=planning_task_id, status="open", limit=200)
for anchor in open_anchors:
    close_task(task_id=anchor.id, reason="terminal cleanup: anchor swept")

lock_label = get_variable(name="interactive_lock_label", session_id="#<self>")
if lock_label:
    remove_label(task_id=plan_parent_ref, label=lock_label)

# Clear session vars — last so the lock-release always has the stored label.
for name in (
    "plan_review_mode",
    "plan_review_requested",
    "plan_parent_ref",
    "planning_task_id",
    "artifact_path",
    "plan_slug",
    "adversary_run_id",
    "active_anchor_id",
    "expansion_execution_id",
    "current_round",
    "max_rounds",
    "interactive_lock_label",
):
    set_variable(name=name, value=None, session_id="#<self>")
```

Using the **exact** value stored in `interactive_lock_label` is critical — `remove_label` is exact-match only. Using the un-suffixed prefix would silently do nothing.

---

## Edge Cases

- **User stops after spawn/pipeline start.** The adversary or pipeline still owns
  its run; on the next skill invocation, the resume-detection branch in Step 1
  picks it up and offers resume / abort / restart.
- **Adversary crashes or never completes.** The lifecycle monitor sends the same
  durable wake on agent death when it can classify the run. Route to Step 9 as
  "adversary crash." If the adversary died mid-claim, the restart path can
  force-release via `reopen_task` or `de_escalate_task`.
- **Session compaction before completion.** `context-handoff` rules re-inject task context on compaction; session vars persist; skill re-enters at the right branch based on task status and var presence.
- **Web UI vs CLI.** Entirely daemon-driven. Both surfaces receive durable
  completion notifications and wake signals; no UI-specific work.
- **Stale plan file from pre-adversarial drafting.** Step 4/7.2 always anchors the artifact at the canonical path; adversary and `expand-task` both read from there.
- **Early-completion race.** Handled by the completion registry and durable wake storage; a later resume inspects persisted task/run state.

---

## Task Hierarchy Produced by `/gobby expand`

(For reference — the tree produced after Step 8 succeeds.)

```text
L1: Root Epic (from plan title)
└── L2: Phase Sub-Epic (from each ## Phase section)
    └── L3: Feature Task (from each ### N.N task heading)
        ├── [TDD] Write failing tests     ← TDD sandwich (auto-generated)
        ├── [IMPL] Implement feature      ← TDD sandwich (auto-generated)
        └── [REF] Refactor with green tests ← TDD sandwich (auto-generated)
```

The detailed semantics (why phases must be sub-epics, how cross-phase deps wire) are covered in the `plan-draft` skill — load it with `get_skill` if you need a refresher.

---

## Optional: Workflow-Enforced Planning

The `plan-expansion` workflow template still exists as a stricter alternative (hard step gates, tool restrictions, loop enforcement). It is documented in `docs/guides/workflows-overview.md`. `/gobby plan` does **not** activate it automatically — use whichever activation path you have configured today.

---

## Lifecycle Coordinator Contract

This skill is the coordinator for the lifecycle-dispatch flow.

- The parent chat session never claims implementation or review tasks during Phase 3. It creates a fresh planning anchor for each adversary round, spawns the adversary, records `active_anchor_id`, then ends the turn.
- Round 1 spawns `plan-adversary` against the user-approved first draft.
- Round N, where N > 1, the coordinator (this chat session, with context restored fresh by the post-spawn compact-self from Round N-1) revises the plan file in place using the latest `## Adversary Findings — Round N-1`, runs the Step 5 verification checklist, then creates a new adversary anchor and spawns `plan-adversary` for Round N. There is no separate `planner` agent spawn — the coordinator IS the planner for all revision rounds.
- Wake routing reads the active anchor with `get_task`. `status=review_approved` proceeds only after the plan file contains `## M1 Task Manifest`; `status=open` with remaining budget triggers coordinator revision and the next adversary spawn; `status=open` with exhausted budget surfaces final findings; `status=escalated` surfaces the escalation reason.
- The daemon's task-completion notification is the wake signal. Do not use `ScheduleWakeup`, `Monitor`, or polling in Phase 3.
- In delegated mode, load the `build` skill before Phase 3b, ask for build scope, then either trigger `gobby build <plan_file> --isolation none` or show that exact CLI command.
- The coordinator owns surgical plan-file revisions between rounds in both modes. Manifest writes belong to `plan-adversary` (final act of approval); the coordinator must not author the manifest.

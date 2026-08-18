# Claude 5-Era Prompt & Instruction Cleanup

**Plan ID:** claude5-prompt-cleanup

> Canonical plan artifact (Lightweight Gobby). Approved via CLI plan mode on 2026-08-17.
> Validation:
> `uv run gobby plans validate .gobby/plans/claude5-prompt-cleanup.md`
> Lightweight Gobby process: no enhancement/adversary/build phases unless opted in.

## Overview
`kind: framing`

Research (Anthropic official docs + 2025–2026 community consensus) shows Gobby's
instruction surfaces use pre-Opus-4.5 prompting patterns that reduce adherence on
Opus 4.5+/Claude 5 models: aggressive emphasis (ALL-CAPS NEVER/ALWAYS) causes
overtriggering; verification/anti-laziness/persona instructions cause over-verification;
bloated always-loaded files get ignored ("Claude ignores half of it"); bare negatives
underperform reason-backed imperatives. Exploration found a four-way doctrine mirror set
with drift (CLAUDE.md 229 lines / AGENTS.md 118 / QWEN.md 256 stale / GUIDING_PRINCIPLES.md
37 unloaded), 25 homogeneous agent YAMLs (5,458 lines, 5–17× "Do NOT" each), a triplicated
persona, and a handful of high-emphasis prompt templates. This epic consolidates to one
canonical AGENTS.md, applies a Claude 5-era style contract across agent/prompt templates,
fixes injection cadence remnants, and adds mechanical design-contract enforcement.

Key sources: code.claude.com/docs/en/memory + best-practices; platform.claude.com
prompting-claude-opus-5 / prompting-claude-fable-5 / claude-prompting-best-practices;
humanlayer.dev writing-a-good-claude-md; paddo.dev claude-md-technical-debt;
claudefa.st what-to-delete-from-claude-md.

## Decision Record (elicited, confirmed)
`kind: framing`

1. AGENTS.md canonical (<200 lines, Claude 5-era style); CLAUDE.md thin wrapper with
   `@AGENTS.md` import (Droid cats both files, wrapper must stay tiny).
2. QWEN.md deleted; named default: point Qwen Code's context filename setting at
   AGENTS.md if supported, else accept the loss.
3. GUIDING_PRINCIPLES.md kept as human-facing narrative (the WHY); no byte-exact mirroring.
4. No doctrine-text tests for AGENTS.md: remove pinning assertions/files.
5. Tone scope: root files + all 25 agent YAMLs (one shared style contract) + LLM prompt
   templates + top-offender skills. Full 70-skill sweep out of scope.
6. Injection: persona already once-per-epoch at first UserPromptSubmit ✓. Remove
   SessionStart short-persona variant; move wiki overview to first UPS; dedup
   compact-handoff wiki duplication; session-row creation timing untouched.
7. Design enforcement: mandate prose to AGENTS.md + new `require-impeccable-skill` rule,
   path-scoped trigger (component/style/markup extensions anywhere; bare .ts/.js/.mjs/.cjs
   only under `web/`); design-detector tightened to same predicate.
8. Perishable facts dropped (display-bug section, test counts, stale principle-1 wording).
9. Gobby persona voice stays (product identity) but single-sourced.
10. Style contract: plain imperatives with reasons; positive phrasing; emphasis budget
    ≤3 bolded rules per file, reserved for destructive-risk rules; no persona blocks in
    worker/task prompts; no verification/anti-laziness exhortation; machine-checkable
    gates stay verbatim; enumerations collapse to brief steering where model defaults
    suffice.

## Constraints
`kind: framing`

- 0.5.0 unshipped — no backward compatibility anywhere.
- Bundled template edits (agents/, prompts/, rules/, skills/) require regenerating
  `src/gobby/install/bundled_content_manifest.json` via `build_bundled_content_manifest`
  (`src/gobby/install/manifest.py`); gate test lives in `tests/test_build_backend.py`.
  Runtime application needs daemon sync (`uv run gobby sync --reinstall` or restart).
- `src/gobby/config/tasks.py` QA prompt strings are mirrored verbatim in
  `crates/gcore/assets/config/runtime_config_contract.json` — change both, and a crate
  change is not live until rebuilt AND reinstalled to `~/.gobby/bin/`.
- Session-row creation timing and SessionStart registration/handoff flows unchanged.
- Out of scope (explicit deferrals not required — never planned): full 70-skill sweep;
  per-turn small reminders (brevity/restraint rules); rule prose beyond the named
  deliverables; `.impeccable.md` content itself (only its enforcement/mandate).
- Nested CLAUDE.mds (`crates/`, `src/gobby/dispatch/`, `src/gobby/install/shared/`,
  `.../workflows/rules/`) remain Claude-Code lazy-load surfaces — tone pass only, no
  relocation.

## P1: Canonical instruction files
`kind: framing`

**Goal**: One canonical AGENTS.md; thin CLAUDE.md wrapper; stale mirrors gone.

### 1.1 Rewrite AGENTS.md as the canonical instruction file [category: docs]
`kind: deliverable`

Targets:
- `AGENTS.md`

Rewrite to <200 lines in the Decision-Record style contract. Structure:

1. Identity + project one-liner (2 lines).
2. **Working rules** — the 18 principles condensed to ~12 reason-backed rules, grouped:
   tool discovery (lease-aware wording from current AGENTS.md principle 1, one line —
   the injected MCP instructions carry the detail); task lifecycle (claim before edit,
   close = linked commit + clean validation run + criteria review, stop hook holds turn
   open while a task is claimed); found-it-fix-it (incl. the shared-worktree exclusion +
   `gobby-agents:send_message` owner notification); monolith ceiling (hook-enforced,
   load `decompose-monolith`, <1,000 lines before commit); plans are decision-complete;
   least mechanism; templates-vs-DB source of truth; prefer gcode; no backward compat;
   agent depth ≤5; `gobby-agents:send_message` for cross-session messaging vs
   `gobby-sessions:send_keys` for terminal control.
   Dropped entirely: sycophancy (#10), never-guess (#15), memory exhortation (#9 — the
   per-session rule nudge covers it), progressive-discovery long section (runtime MCP
   block covers it), display-bug section.
3. Dev commands (uv-only, daemon, ruff/mypy/test-types, pytest patterns).
4. Testing rules: full-suite prohibition (30+ min — one of the ≤3 emphasized rules),
   `GOBBY_TEST_PROTECT=1`, daemon isolation, markers, coverage/CI facts (no test-count
   number — perishable).
5. Repository guidelines from current AGENTS.md: module organization, style/naming,
   commit/PR format (`[gobby-#NNNNN] <type>: <summary>`).
6. Agent workflow (task lifecycle MCP tools; `gobby tasks` CLI is human-only).
7. Architecture facts: templates vs active enforcement (pointer to
   `src/gobby/install/shared/CLAUDE.md`), dispatch pointer, Rust workspace map +
   rebuild-AND-reinstall warning, key file locations table, DB access convention
   (psycopg `%s` in `self.db.transaction()`).
8. Design Context mandate (moved from CLAUDE.md so every CLI sees it): design/UI work
   reads `.impeccable.md` + loads `impeccable` skill; update via teach mode.
9. Plan-coverage contract pointer (`docs/contracts/plan-coverage.md`).

**Acceptance:**

- 1.1.1 - AGENTS.md is under 200 lines, contains the condensed working rules with reasons, and carries no ALL-CAPS emphasis outside at most 3 destructive-risk rules. file: `AGENTS.md`.
- 1.1.2 - The lease-aware tool-discovery wording replaces the stale pre-lease text, and no test-count number appears. file: `AGENTS.md`.
- 1.1.3 - The Design Context mandate and commit/PR format are present. file: `AGENTS.md`.

### 1.2 Shrink CLAUDE.md to a Claude-specific wrapper [category: docs] (depends: 1.1)
`kind: deliverable`

Targets:
- `CLAUDE.md`

Replace the current 229-line file with ≤20 lines: an `@`-import of the canonical
instruction file from deliverable 1.1; the Plan Mode
note (gobby-tasks calls allowed in plan mode — Claude-Code-specific); pointer note that
nested CLAUDE.md files exist under `crates/`, `src/gobby/dispatch/`,
`src/gobby/install/shared/`. Everything else lives in AGENTS.md. The display-bug
workaround section is deleted, not relocated (current harness delivers final-message
guidance natively).

**Acceptance:**

- 1.2.1 - CLAUDE.md is ≤20 lines, imports the canonical file via `@` syntax, and retains the plan-mode note. file: `CLAUDE.md`.

### 1.3 Delete QWEN.md and point Qwen at AGENTS.md [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `QWEN.md`
- `src/gobby/cli/installers/qwen.py::*` — scope-reason: small installer extension; exact symbol chosen at implementation after reading the module

Delete `QWEN.md`. Extend the Qwen installer to merge Qwen Code's context-filename
setting (`contextFileName` / `context.fileName`, verify the current qwen-code settings
schema at implementation) pointing at `AGENTS.md` into the project's `.qwen/settings.json`,
preserving existing user settings. If qwen-code has no such setting, ship only the
deletion (named default from the Decision Record).

**Acceptance:**

- 1.3.1 - QWEN.md no longer exists in the repo. file: `QWEN.md`.
- 1.3.2 - Qwen install writes the context-filename setting when supported, merging not clobbering existing settings. file: `src/gobby/cli/installers/qwen.py`.

### 1.4 Rewrite GUIDING_PRINCIPLES.md as narrative philosophy [category: docs] (depends: 1.1)
`kind: deliverable`

Targets:
- `GUIDING_PRINCIPLES.md`

Human-facing narrative: for each working rule in AGENTS.md, 2–4 sentences of WHY
(design rationale, incidents/history where relevant). No byte-exact duplication of
AGENTS.md text; cross-reference it instead.

**Acceptance:**

- 1.4.1 - GUIDING_PRINCIPLES.md explains the rationale per rule and contains no verbatim copy of the AGENTS.md rules block. file: `GUIDING_PRINCIPLES.md`.

### 1.5 Remove doctrine-text pinning tests [category: refactor] (depends: 1.1)
`kind: deliverable`

Targets:
- `tests/workflows/test_p2p_messaging_guidance.py::test_universal_instruction_files_require_p2p_messaging`
- `tests/workflows/test_monolith_guard.py::test_principle_two_mirrors_require_same_session_decomposition`

Delete `tests/workflows/test_p2p_messaging_guidance.py` entirely (its only test is the
mirror pin). In `tests/workflows/test_monolith_guard.py`, remove
`test_principle_two_mirrors_require_same_session_decomposition` and the
`PRINCIPLE_MIRRORS` tuple; the rest of the suite (monolith rule behavior) stays.

**Acceptance:**

- 1.5.1 - The p2p guidance test file is deleted. file: `tests/workflows/test_p2p_messaging_guidance.py`.
- 1.5.2 - test_monolith_guard.py no longer references PRINCIPLE_MIRRORS and its remaining tests pass. test: `tests/workflows/test_monolith_guard.py`.

### 1.6 Tone-pass nested CLAUDE.md files [category: docs] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/CLAUDE.md`
- `src/gobby/dispatch/CLAUDE.md`
- `src/gobby/install/shared/CLAUDE.md`
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`
- `docs/architecture/source-tree.md`

Apply the style contract (plain imperatives + reasons). Remove content now duplicated
by canonical AGENTS.md (crate→binary map stays in AGENTS.md; `crates/CLAUDE.md` keeps
only Rust-specific detail and defers to AGENTS.md). In `docs/architecture/source-tree.md`,
change the guiding-principles entry description to "design rationale".

**Acceptance:**

- 1.6.1 - Nested CLAUDE.md files carry no content duplicated verbatim from AGENTS.md and follow the style contract. file: `crates/CLAUDE.md`.

## P2: Injection cadence
`kind: framing`

**Goal**: Persona and wiki context inject exactly once per context epoch at first
UserPromptSubmit; no SessionStart persona duplication.

Design facts (verified by code read): the SessionStart short-persona payload is dead
code — `AgentActivationResult.context` is built but no caller injects it (`types.py:12`
already labels it "Legacy activation metadata; not injected at SessionStart"). The epoch
boundary is `_reset_agent_context_injection` (`_session_start/flow.py:146-161`), called
at exactly the three context-loss points: `handle_session_start` mode `"full"`
(clear/compact/first-claim), `handle_pre_created_session` mode `"full"` (Codex/Droid),
and `apply_in_place_compact_context_loss` (Grok — never emits SessionStart, which is why
the wiki block was duplicated into the compact-handoff rule). Cleared variables do NOT
re-arm YAML rule gates automatically; the Python reset is the required re-arm.

### 2.1 Delete the dead SessionStart persona payload [category: refactor]
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/agents.py::activate_default_agent`
- `src/gobby/hooks/event_handlers/_session_start/types.py::AgentActivationResult`
- `tests/hooks/test_session_start_handlers.py::*` — scope-reason: fixture drops the context kwarg and reset-dict assertions change across the suite

In `activate_default_agent`: remove the `identity_parts` build (lines ~273-277, keep the
`time.monotonic()` timing key) and the `context=` kwarg from the `AgentActivationResult`
constructor (~line 322). Remove the `context: str | None` field from
`AgentActivationResult`. Update the `_agent_activation_context()` test fixture
accordingly. No change to `_agent.py` — `_inject_agent_instructions_if_needed` stays the
only persona path.

**Acceptance:**

- 2.1.1 - AgentActivationResult has no context field and activate_default_agent builds no identity text. symbol: `gobby.hooks.event_handlers._session_start.types.AgentActivationResult`.
- 2.1.2 - Session-start handler tests pass with the updated fixture. test: `tests/hooks/test_session_start_handlers.py`.

### 2.2 Move the wiki overview to first-prompt epoch injection [category: code]
`kind: deliverable`

Targets:
- `src/gobby/hooks/event_handlers/_session_start/flow.py::_reset_agent_context_injection`
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml::rules`
- `src/gobby/install/shared/workflows/variables/gobby-default-variables.yaml::*` — scope-reason: add one variable default next to skill_discovery_instructions_shown
- `tests/workflows/test_context_handoff_rules.py::*` — scope-reason: TestInjectWikiOverview event/effect/gating assertions move to turn_start

Rule `inject-wiki-overview` (name kept — renaming would orphan the installed
`rule_definitions` row): `event: session_start` → `turn_start`; `when` gains
`and not variables.get('wiki_overview_injected')`; add a `set_variable
wiki_overview_injected=true` effect after the unchanged `inject_context` template.
New default variable `wiki_overview_injected: false` (no underscore prefix — must be
YAML-effect-writable; verified against `reserved_variables.py`). Re-arm: add
`"wiki_overview_injected": False` to the merged dict in
`_reset_agent_context_injection` — identical epoch semantics to the persona flags,
covering Claude/Codex/Droid full-mode SessionStarts AND Grok in-place compact. Python
wiki seeding (`load_wiki_overview`/`_seed_wiki_overview_var`) stays at session start
unchanged (runs on both flow paths before any turn_start; refreshes per SessionStart).
Side benefit: resumes with live context (mode `"live"`) stop re-injecting the overview.

**Acceptance:**

- 2.2.1 - The wiki rule fires on turn_start gated by wiki_overview_injected and sets the gate in the same evaluation. file: `src/gobby/install/shared/workflows/rules/context-handoff/inject-wiki-overview.yaml`.
- 2.2.2 - _reset_agent_context_injection re-arms the wiki gate alongside the persona flags. symbol: `gobby.hooks.event_handlers._session_start.flow._reset_agent_context_injection`.
- 2.2.3 - Context-handoff rule tests cover the turn_start event, the gated-off case, and the reset re-arm. test: `tests/workflows/test_context_handoff_rules.py`.

### 2.3 Make the wiki rule the sole carrier — dedup compact handoff [category: config] (depends: 2.2)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::rules`
- `tests/hooks/test_session_handoff_handlers.py::*` — scope-reason: in-place-compact reset assertions gain the wiki gate key

In `inject-compact-handoff-on-prompt` only, delete the `{% if wiki_overview %}` block
(lines ~92-100); the session_start variant never had one. Both rules fire in the same
turn_start evaluation post-compact (priority 11 handoff, 13 wiki, ascending sort), so
the wiki lands in the same aggregate context from one template — keeping both would
double-inject. Re-verify the Grok additionalContext budget test.

**Acceptance:**

- 2.3.1 - The on-prompt compact-handoff template no longer contains the wiki block, and the budget test passes. test: `tests/workflows/test_context_handoff_rules.py`.
- 2.3.2 - In-place-compact tests assert the wiki gate resets to false. test: `tests/hooks/test_session_handoff_handlers.py`.

## P3: Prompt style contract and agent fleet
`kind: framing`

**Goal**: One written style contract; all 25 bundled agent YAMLs conform.

### 3.1 Author the prompt style contract [category: docs]
`kind: deliverable`

Targets:
- `docs/contracts/prompt-style.md`

New document codifying Decision Record item 10 with before/after examples taken from
the actual fleet (e.g., a `CRITICAL RULES:` negation list rewritten as reason-backed
imperatives). Covers: emphasis budget, negation→positive conversion, reason attachment,
persona policy (product voice only in default/comms/chat), verification-exhortation ban,
enumeration collapse, machine-checkable gate preservation. Referenced by agent YAML
review going forward.

**Acceptance:**

- 3.1.1 - The contract document exists with at least 3 before/after examples from real fleet files. file: `docs/contracts/prompt-style.md`.

### 3.2 Rewrite orchestrator and reviewer agents [category: config] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml::*` — scope-reason: whole-file prose rewrite of role/instructions/status messages
- `src/gobby/install/shared/workflows/agents/plan-adversary.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/plan-adversary-taskless.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/merge-worker.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/qa-reviewer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/epic-reviewer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/plan-enhancer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/plan-enhancer-taskless.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/doc-reviewer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/planner.yaml::*` — scope-reason: whole-file prose rewrite

Apply the style contract to `role`/`goal`/`instructions` and per-step `status_message`
strings: convert `CRITICAL RULES:` negation lists to reason-backed imperatives, drop
caps headers for sentence-case, collapse enumerations that restate model defaults,
remove the re-check exhortations in plan-adversary/epic-reviewer (adversarial *role
framing* stays — skepticism is the job; exhortation stacking goes). Behavior semantics
(lifecycle steps, tool policy, skill loading) unchanged — this is a prose pass; any
step-flow change is out of scope.

**Acceptance:**

- 3.2.1 - The 10 listed YAMLs contain no `CRITICAL RULES:` header, and per-file "Do NOT" occurrences drop below 5, with each surviving prohibition carrying its reason. file: `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`.
- 3.2.2 - Rule-selector tags, step workflow structure, and tool/skill references are byte-identical before/after (prose-only diff). behavior: "agent step workflows unchanged" in `src/gobby/install/shared/workflows/agents/plan-adversary.yaml`.

### 3.3 Rewrite developer and support agents [category: config] (depends: 3.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/agents/backend-developer.yaml::*` — scope-reason: whole-file prose rewrite of role/instructions/status messages
- `src/gobby/install/shared/workflows/agents/frontend-developer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/fullstack-developer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/trajectory-monitor.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/tech-writer.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/expansion-qa.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/architect.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/product-manager.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/researcher.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/analyst.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/qa-dev.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/goal-taskmaster.yaml::*` — scope-reason: whole-file prose rewrite
- `src/gobby/install/shared/workflows/agents/default.yaml::*` — scope-reason: whole-file prose rewrite preserving persona voice
- `src/gobby/install/shared/workflows/agents/comms-agent.yaml::*` — scope-reason: whole-file prose rewrite preserving persona voice
- `src/gobby/install/shared/workflows/agents/triage-agent.yaml::*` — scope-reason: whole-file prose rewrite

Same pass as 3.2 for the remaining 15 agents. `default.yaml` and `comms-agent.yaml`
keep their persona voice (deliberate product identity per Decision Record 9);
`default.yaml` instructions get the same subtraction pass as the injected block it
feeds (drop restated model defaults, keep platform facts and behavior steering).

**Acceptance:**

- 3.3.1 - The 15 listed YAMLs conform to the style contract with persona voice preserved only in default/comms. file: `src/gobby/install/shared/workflows/agents/default.yaml`.

## P4: LLM prompt templates
`kind: framing`

**Goal**: High-emphasis prompt templates rewritten; persona single-sourced.

### 4.1 De-escalate external validation prompts [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/prompts/external_validation/spawn.md`
- `src/gobby/install/shared/prompts/external_validation/agent.md`
- `src/gobby/install/shared/prompts/external_validation/external.md`

Rewrite in the calmer register the bundled coherence-check validation prompt already
models: state the validator's
job (independent check against criteria, no prior context) in sentence case, drop
"OBJECTIVE and ADVERSARIAL"/"Be CRITICAL"/"Be thorough and skeptical" exhortations —
Opus 5-era models over-verify under them, and hedges suppress findings. Keep the
process steps and output format contracts verbatim.

**Acceptance:**

- 4.1.1 - spawn.md contains no ALL-CAPS emphasis and no thoroughness exhortations while retaining the JSON output contract. file: `src/gobby/install/shared/prompts/external_validation/spawn.md`.

### 4.2 Remove expansion persona block [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/tasks/prompts/expand-task.md`
- `src/gobby/tasks/prompts/expand-task-tdd.md`

Drop "You are a senior technical project manager and architect" (persona contributes
flattery, not capability); open with the task instead. Keep the JSON field table and
example output — those are load-bearing format contracts.

**Acceptance:**

- 4.2.1 - Neither expansion prompt contains an experience-persona opener, and the JSON contract is unchanged. file: `src/gobby/tasks/prompts/expand-task.md`.

### 4.3 Calm the QA-validator config prompts and Rust mirror [category: code]
`kind: deliverable`

Targets:
- `src/gobby/config/tasks.py::TaskValidationConfig`
- `crates/gcore/assets/config/runtime_config_contract.json::*` — scope-reason: two mirrored prompt strings inside the large generated contract JSON

Rewrite the two default prompt strings (lines ~154, ~166): sentence-case, reason-backed
("Only include requirements explicitly stated in the task — invented thresholds create
false failures."). Update the verbatim mirrors in `runtime_config_contract.json`
(entries ~1875, ~1969) to match exactly. Rebuild and reinstall gcore-dependent binaries
so the contract change is live.

**Acceptance:**

- 4.3.1 - Python defaults and Rust contract strings are identical post-change and contain no ALL-CAPS emphasis. symbol: `gobby.config.tasks.TaskValidationConfig`.
- 4.3.2 - Existing config contract tests pass against the updated strings. test: `tests/test_build_backend.py`.

### 4.4 Sentence-case the isolation context banners [category: code]
`kind: deliverable`

Targets:
- `src/gobby/agents/isolation_worktree.py::WorktreeIsolationHandler.build_context_prompt`
- `src/gobby/agents/isolation_clone.py::CloneIsolationHandler.build_context_prompt`

Replace `CRITICAL: Worktree Context` / `CRITICAL: Clone Context` headers with
sentence-case ("Worktree context — you are working in an isolated worktree, not the
main repository") keeping every factual line (paths, branch, merge-back instructions).

**Acceptance:**

- 4.4.1 - Both banners are sentence-case with facts intact; existing isolation tests pass. symbol: `gobby.agents.isolation_worktree.WorktreeIsolationHandler.build_context_prompt`.

### 4.5 Single-source the Gobby persona [category: code] (depends: 3.3)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/prompts/chat/system.md`
- `src/gobby/servers/chat_session_helpers.py::*` — scope-reason: persona-source consolidation touches the chat prompt assembly path in this module
- `src/gobby/servers/websocket/chat/_session.py::*` — scope-reason: chat preamble application site must be reconciled with the single source

Make the `default` agent-definition row the one persona source: chat-session system
prompt assembly builds from `build_prompt_preamble()` on the default agent row (the
websocket chat path already applies agent preambles at `_session.py:679` — reconcile so
persona text is not applied twice). Delete `src/gobby/install/shared/prompts/chat/system.md` and the
`PromptLoader.load("chat/system")` call once the chat-specific instructions it carries
are absorbed into the chat assembly code or the agent row. `_FALLBACK_SYSTEM_PROMPT`
stays as the degraded-mode string.

**Acceptance:**

- 4.5.1 - Exactly one canonical persona text remains (the default agent definition); chat sessions render it once, verified by chat session tests. file: `src/gobby/servers/chat_session_helpers.py`.

## P5: Skills and design-contract enforcement
`kind: framing`

**Goal**: Top-offender skills conform; design mandate mechanically enforced;
templates manifest regenerated.

### 5.1 Emphasis pass on the impeccable skill [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/impeccable/SKILL.md`

Reduce the 33 caps-emphasis hits to the ≤3 budget (deutan-safety and
never-freehand-edit-the-contract are candidates to keep bolded). Convert `You MUST` /
`Do NOT skip this step` scaffolding to reason-backed imperatives. Dispatch table,
steering references, and teach-mode protocol semantics unchanged.

**Acceptance:**

- 5.1.1 - impeccable SKILL.md carries at most 3 emphasized rules and no `You MUST` scaffolding, with dispatch/teach semantics intact. file: `src/gobby/install/shared/skills/impeccable/SKILL.md`.

### 5.2 Remove reviewer persona lines from planning skills [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/plan-review/SKILL.md`
- `src/gobby/install/shared/skills/plan-enhance/SKILL.md`

Drop "You are a rigorous plan reviewer…" / "You are a **constructive,
opportunity-seeking** plan enhancer" openers and the plan-review verification
exhortations; the methodology content stands on its own.

**Acceptance:**

- 5.2.1 - Neither skill opens with a persona line; methodology sections unchanged. file: `src/gobby/install/shared/skills/plan-review/SKILL.md`.

### 5.3 Add require-impeccable-skill rule; tighten the detector [category: config]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/skill-discovery/require-impeccable-skill.yaml`
- `src/gobby/install/shared/workflows/rules/impeccable/design-detector.yaml::rules`
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`

New skill-discovery rule modeled on `require-typescript-skill.yaml`: on first write to
a UI file in a session, `load_skill: impeccable` (once-per-session gate variable). UI
predicate for both this rule and the tightened `impeccable-edit-pass`/`impeccable-deep-pass`:
path ends with `.tsx/.jsx/.vue/.svelte/.astro/.css/.scss/.html` anywhere, OR ends with
`.ts/.js/.mjs/.cjs` AND path starts with `web/` — skill scripts and Node tooling stop
counting as UI edits. Update `src/gobby/install/shared/workflows/rules/CLAUDE.md` group
counts (skill-discovery 24→25).

**Acceptance:**

- 5.3.1 - The new rule exists, tagged for the skill-discovery group, firing once per session on the path-scoped predicate. file: `src/gobby/install/shared/workflows/rules/skill-discovery/require-impeccable-skill.yaml`.
- 5.3.2 - design-detector rules use the same predicate; a write to `src/gobby/install/shared/skills/impeccable/scripts/live-copy-edit-agent.mjs` no longer triggers either. test: `tests/workflows/test_skill_discovery_rules.py`.

### 5.4 Regenerate the bundled content manifest [category: config] (depends: P2, 3.2, 3.3, 4.1, 5.1, 5.2, 5.3)
`kind: deliverable`

Targets:
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: wholesale regeneration of the generated manifest

After all template edits land, regenerate and commit:

```bash
uv run python -c "from pathlib import Path; from gobby.install.manifest import write_bundled_content_manifest; print(write_bundled_content_manifest(Path('src/gobby/install')))"
```

**Acceptance:**

- 5.4.1 - Committed manifest matches the shared tree. test: `tests/install/test_bundled_content_manifest.py::test_bundled_content_manifest_matches_tree`.

## V1: Verification
`kind: verification`

- Materialize + validate the plan: `uv run gobby plans validate .gobby/plans/claude5-prompt-cleanup.md`.
- Focused tests per touched area (never the full suite): `GOBBY_TEST_PROTECT=1 uv run pytest tests/workflows/test_monolith_guard.py tests/workflows/test_skill_discovery_rules.py tests/workflows/test_context_handoff_rules.py tests/hooks/test_session_start_handlers.py tests/hooks/test_session_handoff_handlers.py tests/hooks/test_agent_events_coverage.py tests/install/test_bundled_content_manifest.py tests/test_build_backend.py -v`.
- Lint/type: `uv run ruff check src/ && uv run mypy src/`.
- Apply templates: `uv run gobby sync --reinstall`; restart daemon.
- Live cadence check: fresh Claude session — persona + wiki inject on first prompt only;
  `/clear` then compact → each re-injects exactly once; resume with live context injects
  nothing; no SessionStart persona block.
- Line-count checks: AGENTS.md <200, CLAUDE.md ≤20.
- Rust mirror after 4.3: rebuild, then install via new inode (`cp target/release/<bin>
  ~/.gobby/bin/.<bin>.new && mv -f` — macOS SIGKILLs in-place-overwritten signed binaries).

## Task Mapping
`kind: framing`

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

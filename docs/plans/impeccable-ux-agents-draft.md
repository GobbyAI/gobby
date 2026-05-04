# UX/Design Pipeline via `gobby build` Paradigm

## Context

UX/design work today has no first-class place in the gobby build paradigm — engineering plans, leaves, and validation gates assume code-shaped deliverables. We have `impeccable` as a sophisticated design skill but it lives at the chat-loop level, with no rails to plan, dispatch, audit, or screenshot a UI deliverable through the same dispatch chain that engineering tasks ride.

This plan adds a parallel UX track that reuses every existing rail — `/gobby plan` for planning, plan-coverage contract for acceptance, dispatch stages for routing, `SpawnAgentAction` for execution, plan-adversary for review, expansion for decomposition. No new action types, no new pipeline machinery, no parallel orchestration. Just new agents, new skills, one new stage, and a small artifact-kind extension.

**Scope: web only for v1.** SwiftUI / WinUI / desktop deferred to a follow-up "platformize impeccable" effort. Impeccable's reference files (CSS bans, OKLCH, container queries) are web-coded today; the discipline transfers but the references don't.

**Outcome:** A user can run `/gobby plan` → pick the UX track → produce a UX brief that conforms to plan-coverage contract → adversary writes the manifest → expansion creates leaves → dispatch routes UX-categorized leaves to `ux-developer` (which loads impeccable and uses `craft`/`polish` steering commands) → after development completes, a holistic `ux-review` stage runs `impeccable audit` + `critique` and captures Chrome DevTools screenshots as visual evidence → holistic_qa → pr → merge.

## Approach

### 1. Planner selection in `/gobby plan` (Step 1a as 2×2 menu)

**File:** `src/gobby/install/shared/skills/plan/SKILL.md`

Replace the existing Step 1a binary (adversarial Y/N) with a single 2×2 menu:

```
Step 1a — Track and review:
  1) engineering, plain
  2) engineering, adversarial
  3) ux, plain
  4) ux, adversarial   [Default]
```

Set two session vars from one answer: `planner_track` ∈ {engineering, ux}, `plan_review_requested` ∈ {true, false}.

**Downstream changes in the same skill:**
- Step 3 (drafting): branch on `planner_track` to load `plan-draft` (engineering) or `plan-draft-ux` (UX).
- Step 7.4 (adversary spawn): branch on `planner_track` to spawn `plan-adversary.yaml` or `plan-adversary-ux.yaml`.
- Terminal cleanup: add `planner_track` to the session-var clear list.

**Reuse:** Mode question (Step 6b: interactive vs delegated, max_rounds) is unchanged and applies to both tracks.

### 2. New skills (fork, with shared grammar reference)

**Forking is correct here.** Both `plan-draft` and `plan-review` are large authoritative documents loaded in full into the coordinator's context every time. Parameterizing pollutes the engineering case with UX vocabulary it doesn't need. Skill sync auto-discovers any new directory under `install/shared/skills/` (`src/gobby/skills/sync.py:194`) — fork is free at the registration layer.

**Drift mitigation:** Extract the plan-coverage grammar (deliverable section grammar, M1 manifest schema rules, acceptance-item shape) from `plan-draft/SKILL.md` into a shared reference file (e.g., `plan-draft/references/plan-coverage-grammar.md`). Both `plan-draft` and `plan-draft-ux` load it via `get_skill_file(name="plan-draft", path="references/plan-coverage-grammar.md")`. Editorial voice forks; grammar lives once.

**New files:**
- `src/gobby/install/shared/skills/plan-draft-ux/SKILL.md` — UX authoring methodology. Uses the shared grammar; adds UX-specific deliverable-section archetypes (information architecture, component inventory, interaction model, motion plan, content strategy, visual system). Defines validation_criteria patterns for the new acceptance kinds. Documents the `impeccable shape` steering command as the recommended brief-structuring tool.
- `src/gobby/install/shared/skills/plan-review-ux/SKILL.md` — UX adversarial review heuristics. Mirrors `plan-review` but checks for impeccable anti-patterns: slop test failure, missing brand context (no `.impeccable.md` referenced), generic-template aesthetics, vague interaction descriptions, missing motion/empty/error/loading states, banned-font reflexes.
- `src/gobby/install/shared/skills/plan-draft/references/plan-coverage-grammar.md` — extracted shared grammar reference (move, do not copy).

### 3. New agent definitions

**File pattern:** `src/gobby/install/shared/workflows/agents/`

**New: `ux-developer.yaml`** — base on `frontend-developer.yaml` (already has Playwright/Lighthouse/Storybook in tool_allowlist). Changes from base:
- `instructions`: tailored for design-leaf work and holistic review (impeccable craft for new components, polish/audit/critique for review passes).
- `skills.baseline`: load `impeccable` at agent start; add design-system literacy, accessibility/WCAG, responsive patterns, motion design, UX writing.
- `allowed_mcp_tools`: add `chrome-devtools:*` (server is already bundled — see §8).
- `step_variables`: track `impeccable_mode` (craft vs polish vs audit) per spawn so the prompt builder knows which steering command to invoke.

**New: `plan-adversary-ux.yaml`** — clone `plan-adversary.yaml`. Changes:
- Load `plan-review-ux` instead of `plan-review`.
- Instructions reference the new acceptance kinds (surface, flow, token, motion, content) and their validation predicates.
- Manifest emission rules updated for UX-flavored entries (e.g., `category: design`, validation_criteria patterns specific to design kinds).

### 4. New `ux-review` stage at position 110

**File:** `src/gobby/install/shared/registry/stages.yaml`

Insert between `development` (100) and `holistic_qa` (120):

```yaml
- name: ux-review
  category: design          # already exists in _CATEGORIES (used by architecture/prd)
  position_hint: 110
  default_agent: ux-developer
  review_policy: none       # the audit IS the review; no reviewer agent needed
  display_label: "UX Review"
```

Stage registry sync is hash-based with `ON CONFLICT DO UPDATE`. No DB migration; daemon restart upserts.

### 5. Dispatch rules (single rule, not triplet)

**File:** `src/gobby/dispatch/rules.py`

Two changes:

**(a) Add `ux_review_rule`** following the `ideation_rule` / `architecture_rule` pattern (single rule, not work/review/advance triplet, because `review_policy: none`):
- Spawns `ux-developer` once against the **epic root** with `--no-isolation` (read-only, walks the assembled product).
- Uses `SpawnAgentAction(agent_slug="ux-developer", ...)` with `initial_variables={"impeccable_mode": "audit"}`.
- Stage prompt directs the agent to: walk the product surfaces, run `impeccable audit` and `impeccable critique`, capture Chrome DevTools screenshots per `surface` acceptance item, attach evidence to the epic task, advance the stage on completion.

**(b) Modify `_default_agent` resolution at `rules.py:198`** in `development_rule`. Today: `_default_agent(stage, context) or _field(task, "assigned_agent") or "backend-developer"`. Add a category-aware override: when the leaf's category is `ux` (or any of its acceptance items use a design-flavored artifact kind from §7), the fallback becomes `ux-developer` instead of `backend-developer`. Keep `assigned_agent` precedence intact so manifest authors can still override.

**No new typed action.** `SpawnAgentAction` is sufficient. Task #14138 (generic pipeline-backed stage actions) is orthogonal — defer that dependency.

### 6. PROMPT_BUILDERS registration

**File:** `src/gobby/dispatch/prompts.py:204`

Add two builders to the `PROMPT_BUILDERS` dict:
- `_ux_developer` — produces the agent prompt for `ux-developer` spawns. Branches on `impeccable_mode` initial variable: craft (for development-stage UX leaves), polish/audit (for ux-review stage). Includes the relevant impeccable steering command in the prompt.
- `_plan_adversary_ux` — produces the agent prompt for `plan-adversary-ux` spawns. Mirrors `_plan_adversary` but references UX-flavored review heuristics.

**Tests:** Add cases to `tests/dispatch/test_prompts.py` mirroring `test_discovery_prompt_builders_registered` (which catches missing entries).

### 7. Acceptance-kind extension — 5 kinds

**Files:**
- `src/gobby/plans/parser.py:73` — extend `ArtifactKind` enum with: `surface`, `flow`, `token`, `motion`, `content`. Existing kinds (`file`, `symbol`, `test`, `behavior`) stay.
- `src/gobby/plans/_artifact_refs.py:22` — extend the if/elif chain with five new branches, each with a distinct verification predicate:
  - `surface` → DOM/Storybook presence check (collapses screen + component; scope is editorial and lives in acceptance prose)
  - `flow` → ordered traversal + state-transition invariants (loading/error/empty states must be declared)
  - `token` → design-system file conformance check
  - `motion` → animation spec presence + `prefers-reduced-motion` honored
  - `content` → copy validation against UX-writing rules
- `docs/contracts/plan-coverage.md` (lines 62-63 + the section that describes each kind's semantics) — document the five new kinds and their predicates.
- `src/gobby/plans/manifest_emitter.py` — audit for any whitelist that would reject new kinds; relax if needed.

**No DB migration.** `artifact_kind` is stored as a string field on `AcceptanceItem` and parsed from markdown each time (`parser.py:84`). Enum extension is additive; existing engineering plans keep validating.

### 8. Chrome DevTools MCP wiring

**Already bundled.** `src/gobby/mcp_proxy/bundled.py:15` defines `CHROME_DEVTOOLS_NPM_PACKAGE = "chrome-devtools-mcp@0.21.0"`; `resolve_chrome_devtools_executable_path()` (line 129) handles browser binary location across platforms. Server is registered as `chrome-devtools` in the bundled config (lines 65-69). Browser process is spawned per-MCP-session by the npm package itself.

**Two things to do:**
1. Whitelist `chrome-devtools:*` in `ux-developer.yaml`'s `allowed_mcp_tools`. That's it for plumbing.
2. Establish convention: each `surface` acceptance item produces a screenshot artifact. The `ux-review` stage prompt directs `ux-developer` to capture screenshots via Chrome DevTools MCP and attach them as task evidence (referenced in commit body via a stable label format like `screenshot:<path>`).

**Real risk worth addressing in the plan, not deferring:** Chrome DevTools MCP captures live URLs. The dev server has to be running before screenshot capture. The `ux-review` stage prompt must either (a) start the dev server itself (read command from project config), or (b) require a `dev_server_url` artifact set during expansion. Recommend (b) for v1: add `dev_server_url` to the expected artifacts on UX-track plans; expansion or a pre-`ux-review` rule starts the server and writes the URL; `ux-review` rule reads it.

### 9. No build-profile changes for v1

Existing `quick` / `review` / `full` / `full-yolo` profiles apply uniformly across both tracks. The `ux-review` stage runs whenever it appears in the manifest, regardless of profile. Defer UX-specific profile bundles (`ux-quick`, `ux-review-only`, etc.) to v2 once we have real usage data.

## Critical files to modify

**Skills and agents** (new files, auto-discovered by sync):
- `src/gobby/install/shared/skills/plan-draft-ux/SKILL.md` (new)
- `src/gobby/install/shared/skills/plan-review-ux/SKILL.md` (new)
- `src/gobby/install/shared/skills/plan-draft/references/plan-coverage-grammar.md` (new, extracted from existing `plan-draft/SKILL.md`)
- `src/gobby/install/shared/workflows/agents/ux-developer.yaml` (new, base on `frontend-developer.yaml`)
- `src/gobby/install/shared/workflows/agents/plan-adversary-ux.yaml` (new, base on `plan-adversary.yaml`)

**Existing files to edit:**
- `src/gobby/install/shared/skills/plan/SKILL.md` — Step 1a 2×2 menu; Step 3 + Step 7.4 branch on `planner_track`; cleanup adds `planner_track`.
- `src/gobby/install/shared/skills/plan-draft/SKILL.md` — extract grammar to shared reference; load via `get_skill_file`.
- `src/gobby/install/shared/registry/stages.yaml` — add `ux-review` entry at position 110.
- `src/gobby/dispatch/rules.py` — add `ux_review_rule` (single rule); modify `_default_agent` resolution in `development_rule` for UX-categorized leaves.
- `src/gobby/dispatch/prompts.py` — add `_ux_developer` + `_plan_adversary_ux` to `PROMPT_BUILDERS`.
- `src/gobby/plans/parser.py` — extend `ArtifactKind` enum with 5 new kinds.
- `src/gobby/plans/_artifact_refs.py` — extend if/elif chain with 5 verification predicates.
- `src/gobby/plans/manifest_emitter.py` — audit for kind whitelisting; relax if present.
- `docs/contracts/plan-coverage.md` — document new acceptance kinds and predicates.

**Tests to add:**
- `tests/dispatch/test_prompts.py` — registration cases for new builders.
- `tests/plans/test_parser.py` — round-trip cases for new artifact kinds.
- `tests/plans/test_artifact_refs.py` — verification-predicate cases for each new kind.
- `tests/dispatch/test_rules.py` — `ux_review_rule` triggering; `_default_agent` UX category override.
- New `tests/install/test_skills_sync.py` case (if one doesn't exist) confirming new skill dirs auto-discover.

## Reused existing utilities (do not reinvent)

- `SpawnAgentAction` (`src/gobby/dispatch/actions.py`) — vehicle for spawning ux-developer; no new action class.
- `_default_agent()` resolver (`src/gobby/dispatch/rules.py:198`) — extend, don't fork.
- Skill auto-discovery (`src/gobby/skills/sync.py:194`) — drop new SKILL.md dirs in place; daemon restart picks them up.
- Stage registry sync with `ON CONFLICT DO UPDATE` — adding to `stages.yaml` is enough.
- Bundled Chrome DevTools MCP (`src/gobby/mcp_proxy/bundled.py:15`) — whitelist in agent, no new server config.
- `frontend-developer.yaml` tooling baseline (Playwright/Lighthouse/Storybook) — base `ux-developer.yaml` on it.
- `plan-adversary.yaml` and `plan-review` skill structure — clone for UX track, swap methodology.
- `expand-task` pipeline (`src/gobby/install/shared/workflows/pipelines/expand-task.yaml`) — works as-is; takes plan file as input regardless of track.

## Verification

End-to-end test (manual, in a scratch project):
1. **Planner-choice surfacing:** Run `/gobby plan`, observe Step 1a renders the 4-option menu, pick option 4 (ux + adversarial), confirm `planner_track=ux` and `plan_review_requested=true` in session vars.
2. **UX drafting:** Confirm Step 3 loads `plan-draft-ux` (not `plan-draft`). Draft a small UX brief (e.g., "redesign the settings panel") and run `gobby plans validate <plan-file>` — confirm new acceptance kinds (`surface`, `flow`, `token`, `motion`, `content`) parse without error.
3. **UX adversary:** Confirm Step 7.4 spawns `plan-adversary-ux` (not `plan-adversary`). Confirm manifest writes successfully with UX entries.
4. **Expansion:** Run expansion against the plan; confirm leaves are created with `category: design` and `assigned_agent: ux-developer` (or unset, with category override fallback).
5. **Build dispatch:** `gobby build <epic>` with profile `review`. Confirm stages render with `ux-review` at position 110 between `development` and `holistic_qa` in `task_stage_states`.
6. **Development routing:** Confirm a UX-categorized leaf in development gets routed to `ux-developer` (not `backend-developer`) via the `_default_agent` override; confirm impeccable skill loads at agent start.
7. **ux-review execution:** Confirm `ux-review` stage spawns `ux-developer` once against the epic root with `impeccable_mode=audit`, runs Chrome DevTools MCP for screenshots, attaches evidence to the task. Verify `chrome-devtools:*` tools are accessible to the agent.
8. **Stage advancement:** Confirm `ux-review` advances on agent completion (no reviewer needed since `review_policy: none`); `holistic_qa` proceeds.
9. **Coverage closure:** `gobby plans coverage --plan <file> --plan-id <id> --plan-hash <sha>` exits 0 with all rows `status: covered`.

Automated tests:
- `uv run pytest tests/plans/ -v` — confirms parser, artifact-refs, manifest changes.
- `uv run pytest tests/dispatch/ -v` — confirms `ux_review_rule`, prompt-builder registration, agent routing.
- `uv run pytest tests/install/ -v` — confirms skills/agents/stages auto-sync.

Lint and type:
- `uv run ruff check src/`
- `uv run mypy src/`

## Out of scope (explicit deferrals)

- Native platform support (SwiftUI, WinUI, desktop). Filed as follow-up: "platformize impeccable" — split impeccable into core discipline + platform packs (web, swiftui, winui, tui).
- Generic pipeline-backed stage action support (#14138). Not needed for v1; revisit after.
- Build profile bundles for UX-only flows (`ux-quick`, `ux-review-only`). Defer to v2.
- Visual regression diffing (snapshot comparison across runs). v1 captures screenshots as evidence; comparison infrastructure deferred.
- Dev-server lifecycle automation. v1 reads `dev_server_url` from artifacts; orchestrating `npm run dev` from gobby is a separate concern.

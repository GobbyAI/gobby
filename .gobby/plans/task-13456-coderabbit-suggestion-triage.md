# CodeRabbit Suggestion Triage on 0.4.0 Branch

**Plan ID:** task-13456-coderabbit-suggestion-triage

## Overview
`kind: framing`

Triage a batch of CodeRabbit suggestions posted against the 0.4.0 branch.
Apply real bugs (invalid CSS `var(--x)NN` syntax that browsers ignore, async
I/O blocking event loops, inverted `can_resume` logic, a retired Gemini model
ID, invisible button text where `background` equals `color`), apply worthwhile
nits (markdown lint, redundant `var(x, x)` self-fallbacks, inclusivity in
nano-banana skill recipes, a contradictory photo-restoration constraint, test
cleanups), and document false positives that are NOT applied (intentional
code-span spaces, intentional tilde fences around nested code blocks,
intentional shell-injection-risk in `verify_in_worktree` by design, project-
wide design-system contrast pattern).

## Constraints
`kind: framing`

- **No expansion.** Per user directive, this plan is reviewed by
  `plan-adversary` for one round, approved, and then the fixes are applied
  manually by the parent session. `/gobby expand` is NOT invoked. The plan
  exists primarily so the adversary can walk every fix's branch and surface
  things the triage missed.
- **No new bug-introducing fixes.** Every accepted fix MUST preserve the
  observable behavior of code paths it does not touch. Fixes are surgical
  edits to the lines CodeRabbit flagged, not refactors.
- **Each rejected suggestion carries a written reason** in §X1 so the next
  reviewer doesn't relitigate the call.
- **`var(--x)NN` is not a CSS thing.** Browsers parse `var(--color-error)33`
  as a syntax error and discard the entire declaration. Every fix in §P2
  replaces this with `color-mix(in srgb, var(--color-error) <pct>%,
  transparent)` to produce the originally-intended translucent shade.
- **Async I/O fixes use `asyncio.to_thread(...)`** rather than introducing an
  `aiofiles` dependency. The repo doesn't currently depend on aiofiles and
  the call sites are infrequent (per-file, not per-line); thread-pool dispatch
  is the appropriate weight.
- **`/gobby plan` Step 8 expand-task pipeline is skipped.** After Step 6
  approval and §X9 adversary verdict, the parent runs terminal cleanup and
  applies fixes directly. The planning epic is closed with reason "approved;
  user applying fixes manually without expansion."

## P1 Phase 1: Python and YAML bug fixes
`kind: framing`

**Goal**: fix three async I/O / logic / config bugs in the merge pipeline and
the gemini-image-gen agent.

### 1.1 Wrap blocking file write in merge.py async path [category: code]
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/merge.py:344-346`

Current code calls `target.write_text(conflict.resolved_content)` directly
inside `merge_apply` (an `async def`). Synchronous file I/O blocks the asyncio
event loop. Wrap both the `mkdir` and `write_text` in `asyncio.to_thread`.
The `asyncio` module is already imported at line 17.

```python
target = Path(wt_path) / conflict.file_path
await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
await asyncio.to_thread(target.write_text, conflict.resolved_content)
```

**Acceptance:**

- 1.1.1 — Both `target.parent.mkdir(...)` and `target.write_text(...)` in
  `merge_apply` are dispatched via `await asyncio.to_thread(...)`. file:
  `src/gobby/mcp_proxy/tools/merge.py`.
- 1.1.2 — `merge_apply` remains the same `async def` with the same return
  shape; behavior change is purely event-loop hygiene. symbol:
  `gobby.mcp_proxy.tools.merge.create_merge_registry.merge_apply`.

### 1.2 Wrap blocking file read in resolver.py tier-2 path [category: code]
`kind: deliverable`

Target: `src/gobby/worktrees/merge/resolver.py:524-529`

`_resolve_conflicts_only` reads the conflicted file via
`Path(file_path).read_text()` directly inside an `async` function. Wrap the
call in `asyncio.to_thread`. Preserve the existing `OSError` handling — the
exception now bubbles out of the awaited call, so the surrounding
`try`/`except` stays in place.

```python
try:
    file_with_markers = await asyncio.to_thread(
        Path(file_path).read_text
    )
except OSError as read_err:
    logger.error(
        f"Failed to read {file_path} for hunk splicing: {read_err}"
    )
    return {"success": False, "resolutions": []}
```

**Acceptance:**

- 1.2.1 — `Path(file_path).read_text()` is replaced by
  `await asyncio.to_thread(Path(file_path).read_text)` inside
  `_resolve_conflicts_only`. file: `src/gobby/worktrees/merge/resolver.py`.
- 1.2.2 — The `OSError` handler around the read still logs and returns the
  `{"success": False, "resolutions": []}` failure dict. symbol:
  `gobby.worktrees.merge.resolver.MergeResolver._resolve_conflicts_only`.

### 1.3 Fix inverted can_resume logic and log rev-list failures [category: code]
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/merge_landscape.py:112-120, 445-455`

Two coupled fixes in `merge_landscape.py`:

**Fix A — `inspect_merge_state` `can_resume` logic (line 445).** Current:
`can_resume = bool(conflicted_files) and state != "clean"` returns `False`
for a `state="merging"` worktree with all conflicts staged but uncommitted —
that case IS resumable (operator runs `git commit` or `git merge --continue`).
Flip AND→OR: `can_resume = bool(conflicted_files) or state != "clean"`.

**Fix B — `analyze_merge_landscape` warning on `rev-list` failure
(line 114).** When `_git_async(["rev-list", "--count", f"{base_ref}...HEAD"])`
returns non-zero, the current code silently sets `divergence_commits = None`,
hiding the difference between "no divergence" and "couldn't compute
divergence". Add a warning log before the fallback so an unfetched base
branch is diagnosable. Module already has `logger` at line 25.

```python
rc, stdout, stderr = await _git_async(
    git_manager,
    ["rev-list", "--count", f"{base_ref}...HEAD"],
    cwd=wt_path,
)
if rc == 0 and stdout.strip().isdigit():
    entry["divergence_commits"] = int(stdout.strip())
else:
    logger.warning(
        "rev-list failed for worktree %s (base=%s): rc=%d stderr=%s; "
        "the base branch may not be fetched in this worktree.",
        wt_path, base_ref, rc, stderr.strip(),
    )
    entry["divergence_commits"] = None
```

**Acceptance:**

- 1.3.1 — `inspect_merge_state` returns `can_resume=True` when EITHER
  `conflicted_files` is non-empty OR `state != "clean"`. symbol:
  `gobby.mcp_proxy.tools.merge_landscape.register_merge_landscape_tools.inspect_merge_state`.
- 1.3.2 — `analyze_merge_landscape` logs a warning naming `wt_path`,
  `base_ref`, `rc`, and stderr when `rev-list` fails before falling back to
  `divergence_commits=None`. file:
  `src/gobby/mcp_proxy/tools/merge_landscape.py`.
- 1.3.3 — Existing test
  `tests/mcp_proxy/tools/test_merge_landscape.py::test_inspect_merge_state_clean`
  still passes (clean worktree with no conflicts → `can_resume=False`). test:
  `tests/mcp_proxy/tools/test_merge_landscape.py::test_inspect_merge_state_clean`.

### 1.4 Update gemini-image-gen agent to a current Gemini model [category: config]
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/gemini-image-gen.yaml:12`

`model: gemini-2.0-flash-exp` is a retired experimental model id. The actual
image generation goes through the nano-banana extension (whose default is
`gemini-2.5-flash-image` per
`src/gobby/install/shared/skills/nano-banana/SKILL.md:114`). The agent-level
`model` is for orchestration text only. Replace with `gemini-2.5-flash`
(current GA, matches what other gobby agents use).

```yaml
provider: gemini
model: gemini-2.5-flash
```

**Acceptance:**

- 1.4.1 — `model:` field on the `gemini-image-gen` agent definition reads
  `gemini-2.5-flash`. file:
  `src/gobby/install/shared/workflows/agents/gemini-image-gen.yaml`.
- 1.4.2 — `provider: gemini` and the rest of the agent definition are
  unchanged. file:
  `src/gobby/install/shared/workflows/agents/gemini-image-gen.yaml`.

## P2 Phase 2: CSS bug fixes (browser-fatal syntax + invisible text)
`kind: framing`

**Goal**: replace invalid `var(--x)NN` constructs with `color-mix(...)`,
restore contrast on the stop-button and pipeline approve/reject buttons, and
remove dead `var(x, x)` self-fallbacks.

### 2.1 Replace invalid var(--x)NN with color-mix in ConfigurationPage.css [category: refactor]
`kind: deliverable`

Target: `web/src/components/ConfigurationPage.css:100, 104, 105, 121, 122, 374, 543-549`

Six occurrences of `var(--token)NN` that browsers ignore as a syntax error.
Replace each with the equivalent `color-mix(in srgb, var(--token) <pct>%,
transparent)`. Translation table:

| Line | Suffix | Percent (color-mix) |
| --- | --- | --- |
| 100 | `33` | 20% |
| 104 | `15` | 8% |
| 105 | `66` | 40% |
| 121 | `15` | 8% |
| 122 | `33` | 20% |
| 374 | `66` | 40% |
| 548 | `15` | 8% |
| 549 | `15` | 8% |

The percentages are derived from the originally-intended hex alpha (`33` ≈
0.2 = 20%, `15` ≈ 0.083 ≈ 8%, `66` ≈ 0.4 = 40%) so the visual outcome matches
the broken-but-intended design.

**Acceptance:**

- 2.1.1 — No occurrences of the regex `var\(--[a-z-]+\)\d+` remain in
  `web/src/components/ConfigurationPage.css`. file:
  `web/src/components/ConfigurationPage.css`.
- 2.1.2 — Each affected declaration uses `color-mix(in srgb, var(--token)
  <pct>%, transparent)` with the percent from the translation table. file:
  `web/src/components/ConfigurationPage.css`.
- 2.1.3 — Visual smoke test: `.config-restart-banner`, `.config-toolbar-btn.danger`,
  the danger-secret button, and `.config-prompt-badge.bundled/.overridden` all
  render with their intended translucent backgrounds in light + dark themes.
  behavior: "translucent badges and danger buttons render correctly" in
  `web/src/components/ConfigurationPage.css`.

### 2.2 Replace invalid var(--x)NN with color-mix in task-advanced.css [category: refactor]
`kind: deliverable`

Target: `web/src/components/tasks/task-advanced.css:932-933, 1079-1080`

Same bug class in two pinned-card rules. Both use `var(--color-warning-foreground)40`
for border (≈25%) and `var(--color-warning-foreground)08` for background (≈3%).
Replace with color-mix using percentages that match the rest of the file's
warning rules (line 215 already uses 15%, line 256 6%, line 408 6%/30%).

```css
.task-memory-item--pinned {
  border-color: color-mix(in srgb, var(--color-warning-foreground) 25%, transparent);
  background: color-mix(in srgb, var(--color-warning-foreground) 3%, transparent);
}
.memory-card--pinned {
  border-color: color-mix(in srgb, var(--color-warning-foreground) 25%, transparent);
  background: color-mix(in srgb, var(--color-warning-foreground) 3%, transparent);
}
```

**Acceptance:**

- 2.2.1 — `.task-memory-item--pinned` (line ~932) uses `color-mix` for
  border-color and background. file:
  `web/src/components/tasks/task-advanced.css`.
- 2.2.2 — `.memory-card--pinned` (line ~1079) uses `color-mix` for the same
  two properties. file: `web/src/components/tasks/task-advanced.css`.
- 2.2.3 — No `var\(--[a-z-]+\)\d+` regex matches remain in the file. file:
  `web/src/components/tasks/task-advanced.css`.

### 2.3 Restore contrast on .stop-button text [category: refactor]
`kind: deliverable`

Target: `web/src/components/chat/styles/input.css:356-364`

`.stop-button` sets `background: var(--color-error)` AND `color: var(--color-error)`,
so the icon/glyph is invisible. The `:hover` rule has the same defect.
Change `color` to a contrasting white on both rules; keep
`background`/`border` as the error token for the destructive affordance.

```css
.stop-button {
  background: var(--color-error);
  color: #fff;
  border: 1px solid var(--color-error);
}

.stop-button:hover {
  background: var(--color-error);
  color: #fff;
}
```

**Acceptance:**

- 2.3.1 — `.stop-button` sets `color: #fff` (or an equivalent on-error token
  if added separately). file:
  `web/src/components/chat/styles/input.css`.
- 2.3.2 — `.stop-button:hover` sets the same contrasting `color`. file:
  `web/src/components/chat/styles/input.css`.
- 2.3.3 — Visual smoke test: the stop glyph is visible on both default and
  hover states in the dark theme. behavior: "stop button glyph is readable"
  in `web/src/components/chat/styles/input.css`.

### 2.4 Restore contrast on pipeline approve/reject buttons [category: refactor]
`kind: deliverable`

Target: `web/src/components/workflows/PipelinesPage.css:345-363`

`.pipeline-btn--approve` and `.pipeline-btn--reject` set background, border,
AND color to the same token, hiding the labels. Set `color: #fff` on the base
rules; remove the redundant `color` from the `:hover` rules (color carries
from the base rule).

```css
.pipeline-btn--approve {
  background: var(--color-success-foreground);
  border: 1px solid var(--color-success-foreground);
  color: #fff;
}

.pipeline-btn--reject {
  background: var(--color-error);
  border: 1px solid var(--color-error);
  color: #fff;
}
```

**Acceptance:**

- 2.4.1 — `.pipeline-btn--approve` and `.pipeline-btn--reject` set `color: #fff`
  on the base rule. file:
  `web/src/components/workflows/PipelinesPage.css`.
- 2.4.2 — The light-theme overrides at lines 113-129 of the same file already
  use `--color-success-foreground` / `--color-error` for color over a soft
  background; verify they still produce ≥ 4.5:1 contrast and no hover regression.
  behavior: "approve/reject button labels are readable in both themes" in
  `web/src/components/workflows/PipelinesPage.css`.

### 2.5 Remove redundant var(x, x) self-fallbacks [category: refactor]
`kind: deliverable`

Target: `web/src/components/rules/RuleEditForm.css:241-243`;
`web/src/components/workflows/LaunchAgentModal.css:323`

Three `var(--token, var(--token))` occurrences where the fallback is the same
variable as the primary. The fallback is dead syntax. Drop the inner `var()`.

```css
/* RuleEditForm.css:241 */
color: var(--color-warning-foreground);

/* LaunchAgentModal.css:323 */
color: var(--color-success-foreground);
```

For `RuleEditForm.css:242-243`, the pattern is
`var(--color-warning, var(--color-warning-foreground))` — that fallback IS
distinct from the primary. Keep that one only if `--color-warning` is defined
in the token set; otherwise replace with `var(--color-warning-foreground)`.
Verify by grepping `web/src/styles/` for `--color-warning:` (without the
`-foreground` suffix).

**Acceptance:**

- 2.5.1 — `RuleEditForm.css` `.rule-edit-conflict-warning` declarations no
  longer contain `var(--token, var(--token))` self-fallbacks; the
  `--color-warning, --color-warning-foreground` fallback is preserved iff
  `--color-warning` actually exists. file:
  `web/src/components/rules/RuleEditForm.css`.
- 2.5.2 — `LaunchAgentModal.css:323` `.launch-agent-success-icon` uses
  `color: var(--color-success-foreground)` with no self-fallback. file:
  `web/src/components/workflows/LaunchAgentModal.css`.

## P3 Phase 3: Documentation and skill cleanups
`kind: framing`

**Goal**: neutralize ethnicity defaults in nano-banana recipes, fix one
contradictory constraint, and clean up two markdown lint nits.

### 3.1 Neutralize ethnicity defaults in nano-banana recipes [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/nano-banana/references/anime-to-life.md:14-15`;
`src/gobby/install/shared/skills/nano-banana/references/figure-to-life.md:36-46`;
`src/gobby/install/shared/skills/nano-banana/references/j-idol.md:11-13`

Three nano-banana creative recipes hard-code "Russian or Japanese" ethnicity
selection (one with broken grammar: "determine by identify"). Replace with
neutral phrasing that lets the model choose appropriate features based on the
character's depicted traits, NOT a presumed ethnic archetype.

**`anime-to-life.md:14-15`** — replace the "Russian or Japanese, determine by
identify" line with:
> "Select an appropriate ethnicity based on the character's depicted traits."

**`figure-to-life.md:36-46`** — remove the `ethnicity_preference` block
(`western_archetype` / `asian_archetype` / `logic`) entirely. Replace with a
neutral `appearance_preferences` field:
```json
"appearance_preferences": {
  "logic": "Match the character's depicted traits (skin tone, hair, eye color, build) without applying ethnic-archetype presets."
}
```
Keep `gender`, `body_type`, `eye_color`, `hair_color`, `skin_tone` as
`STRICT_MATCH_CHARACTER_LORE`. Those are character-fidelity fields, not
ethnicity presets.

**`j-idol.md:11-13`** — replace the two "Russian woman" / "Japanese woman"
bullets with one bullet:
> "Anime/Game CGI: use a young adult female face matching the character's
> depicted features and heritage."

**Acceptance:**

- 3.1.1 — `anime-to-life.md` no longer contains the "Russian or Japanese"
  sentence; replacement names "depicted traits". file:
  `src/gobby/install/shared/skills/nano-banana/references/anime-to-life.md`.
- 3.1.2 — `figure-to-life.md` no longer contains keys `western_archetype`,
  `asian_archetype`, or the `Apply_preference_based_on_character_origin`
  logic string. file:
  `src/gobby/install/shared/skills/nano-banana/references/figure-to-life.md`.
- 3.1.3 — `j-idol.md` ethnicity bullets are merged into one neutral bullet
  with no "Russian" or "Japanese" hard-codes. file:
  `src/gobby/install/shared/skills/nano-banana/references/j-idol.md`.

### 3.2 Fix contradictory constraint in photo-restoration.md [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/nano-banana/references/photo-restoration.md:26`

The current "Constraint" line says
> "While largely improving the quality of the photo, the restored image
> should remain identical to the original."

Improving quality and remaining identical to the original are contradictory.
Replace with:
> "While improving quality dramatically, preserve the original composition,
> identity, pose, and framing."

**Acceptance:**

- 3.2.1 — The "Constraint" bullet on line 26 names "preserve the original
  composition, identity, pose, and framing" instead of "remain identical to
  the original". file:
  `src/gobby/install/shared/skills/nano-banana/references/photo-restoration.md`.

### 3.3 Markdown lint cleanup in nano-banana SKILL.md [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/nano-banana/SKILL.md:115-119`

MD031 — fenced code block needs a blank line before the opening fence. The
"For higher quality (4K, better reasoning):" paragraph is followed
immediately by ` ```bash ` with no blank line.

**Acceptance:**

- 3.3.1 — A blank line separates the prose paragraph from the ` ```bash `
  fenced block in `nano-banana/SKILL.md`. file:
  `src/gobby/install/shared/skills/nano-banana/SKILL.md`.

### 3.4 Add blank lines after j-cover.md headings [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/nano-banana/references/j-cover.md:7-51`

MD022 — every ATX heading needs a blank line after it. Insert blank lines
after the seven affected headings: `# PREREQUISITE`, `# INPUT IMAGE:`,
`# STEP 0: LAYOUT ANALYSIS`, `# STEP 1: PRECISE OUTPAINTING & COMPOSITION`,
`# STEP 2: TYPOGRAPHY & LAYOUT`, `# STEP 3: GRAPHIC ELEMENTS`, `# OUTPUT GOAL`.

**Acceptance:**

- 3.4.1 — Each of the seven headings in `j-cover.md` is followed by a blank
  line before its body content. file:
  `src/gobby/install/shared/skills/nano-banana/references/j-cover.md`.

### 3.5 Clarify cherry-pick recovery in merge-expert SKILL.md [category: docs]
`kind: deliverable`

Target: `src/gobby/install/shared/skills/merge-expert/SKILL.md:147-150`

Recovery for `state == "cherry-picking"` already notes "no MCP wrapper
today; document as a worker delegation" but doesn't explicitly say WHO runs
`git cherry-pick --continue`. Make the worker-delegation requirement
explicit:

> `state == "cherry-picking"` with conflicts → call `gobby-merge:merge_resolve`
> on each conflict, then **dispatch a `merge-worker` to run
> `git cherry-pick --continue`** in the worktree (no MCP wrapper today;
> worker delegation is required because the orchestrator is read+dispatch
> only).

**Acceptance:**

- 3.5.1 — The cherry-picking recovery bullet in `merge-expert/SKILL.md`
  explicitly names "dispatch a `merge-worker`" as the actor for
  `git cherry-pick --continue`. file:
  `src/gobby/install/shared/skills/merge-expert/SKILL.md`.

## P4 Phase 4: Test cleanups
`kind: framing`

**Goal**: minor test hygiene — simplify a case check, document a contractual
assertion, add a reverse-direction regex test, and replace a private-attribute
mutation with a public constructor argument.

### 4.1 Simplify case checks in test_plan_adversary_self_check.py [category: refactor]
`kind: deliverable`

Target: `tests/agents/test_plan_adversary_self_check.py:79-84`

`test_yolo_never_escalates_after_cap` already creates a lowercased `lowered`
variable but then mixes case-sensitive and case-insensitive checks. Drop the
case-sensitive `"do NOT" in instructions` clause; rely on `lowered` for all
substring presence assertions.

```python
def test_yolo_never_escalates_after_cap(self, agent: AgentDefinitionBody) -> None:
    instructions = agent.instructions or ""
    lowered = instructions.lower()
    assert "yolo" in lowered
    assert "never" in lowered or "do not" in lowered
    assert "escalate" in lowered
```

**Acceptance:**

- 4.1.1 — The test reads only `lowered` for all three substring checks;
  no `"do NOT" in instructions` clause remains. test:
  `tests/agents/test_plan_adversary_self_check.py::TestYoloFallback::test_yolo_never_escalates_after_cap`.

### 4.2 Document orchestrator provider/isolation invariants [category: docs]
`kind: deliverable`

Target: `tests/integration/test_merge_orchestrator.py:263-264`

The test asserts `data["provider"] == "codex"` and `data["isolation"] == "none"`.
These are intentional contractual values: the orchestrator runs on codex (no
LLM costs for routing logic) and never gets an isolation worktree (it
dispatches workers; doesn't edit code). Add a comment explaining the
invariants so future maintainers don't relax them as "fragile".

```python
# Contract: orchestrator runs on codex (cheap routing-only LLM) and never
# gets isolation (it dispatches workers; doesn't edit code itself).
assert data["provider"] == "codex"
assert data["isolation"] == "none"
```

**Acceptance:**

- 4.2.1 — A comment immediately above the two assertions documents both
  invariants and their rationale. test:
  `tests/integration/test_merge_orchestrator.py::test_orchestrator_yaml_loads`.

### 4.3 Extend granularity regex coverage with reverse-direction parametrize [category: refactor]
`kind: deliverable`

Target: `tests/servers/routes/admin/test_token_timeseries_route.py`

The existing test module covers two directions: every member of
`VALID_GRANULARITIES` is accepted by the regex AND obvious non-members are
rejected. Extend the existing module with a third parametrized assertion:
whenever `pattern.fullmatch(candidate)` is true, the candidate MUST be a
member of `VALID_GRANULARITIES`. Catches a future regex change that admits
non-members the route would later reject at `_coerce_granularity`. This is
test-suite extension on an existing module, not introduction of a new
test surface.

```python
@pytest.mark.parametrize(
    "candidate",
    list(VALID_GRANULARITIES) + ["", "5m", "2h", "1H", "1d1h", "30s", "1y"],
)
def test_route_pattern_only_matches_members(
    candidate: str, granularity_pattern: str
) -> None:
    pattern = re.compile(granularity_pattern)
    if pattern.fullmatch(candidate):
        assert candidate in VALID_GRANULARITIES, (
            f"route pattern {pattern.pattern!r} accepted non-member "
            f"{candidate!r}; if intended, add it to VALID_GRANULARITIES."
        )
```

**Acceptance:**

- 4.3.1 — `tests/servers/routes/admin/test_token_timeseries_route.py` defines
  `test_route_pattern_only_matches_members` parametrized over a representative
  candidate list. test:
  `tests/servers/routes/admin/test_token_timeseries_route.py::test_route_pattern_only_matches_members`.

### 4.4 Add public llm_service kwarg to MergeResolver [category: code]
`kind: deliverable`

Target: `src/gobby/worktrees/merge/resolver.py:199-213`;
`tests/worktrees/merge/test_resolver_content_flow.py:109-113`

`MergeResolver.__init__` doesn't accept `llm_service` or `_config`, so the
test fixture mutates `_llm_service` directly, coupling tests to a private
attribute. Add optional kwargs to the constructor and update the fixture.

```python
def __init__(
    self,
    conflict_size_threshold: int = 100,
    max_parallel_files: int = 5,
    *,
    llm_service: "LLMService | None" = None,
    config: Any | None = None,
) -> None:
    self.conflict_size_threshold = conflict_size_threshold
    self.max_parallel_files = max_parallel_files
    self._llm_service = llm_service
    self._config = config
```

```python
@pytest.fixture
def resolver_with_llm() -> MergeResolver:
    return MergeResolver(llm_service=MagicMock())
```

Validation criteria: every existing call site of `MergeResolver()` continues
to work (the kwargs are optional with safe defaults).

**Acceptance:**

- 4.4.1 — `MergeResolver.__init__` accepts optional keyword-only `llm_service`
  and `config`. symbol: `gobby.worktrees.merge.resolver.MergeResolver`.
- 4.4.2 — `tests/worktrees/merge/test_resolver_content_flow.py::resolver_with_llm`
  passes `llm_service=MagicMock()` to the constructor and no longer mutates
  `_llm_service` directly. test:
  `tests/worktrees/merge/test_resolver_content_flow.py::resolver_with_llm`.

## P5 Phase 5: Web/CSS nits
`kind: framing`

**Goal**: small UX/semantic improvements.

### 5.1 Stable composite key for label list in TasksTabDetailPanel [category: code]
`kind: deliverable`

Target: `web/src/components/activity/TasksTabDetailPanel.tsx:99-103`

`labels.map((label) => <span key={label} ...>)` produces React duplicate-key
warnings if two coverage labels happen to repeat. Use a composite key:

```tsx
{labels.map((label, index) => (
  <span key={`${label}-${index}`} className="activity-task-detail-label">
    {label}
  </span>
))}
```

Validation criteria: the rendered output is unchanged; the only difference is
React's reconciliation key.

**Acceptance:**

- 5.1.1 — `labels.map` callback signature includes `index` and `key` is
  `${label}-${index}`. file:
  `web/src/components/activity/TasksTabDetailPanel.tsx`.

### 5.2 Use border token instead of bg-tertiary for agent card hover [category: refactor]
`kind: deliverable`

Target: `web/src/components/agents/agents.css:399-401`

`.agent-def-card:hover { border-color: var(--bg-tertiary); }` uses a
background token as a border color, which is semantically wrong. Switch to
`var(--text-muted)` to match the convention used by `.workflows-card:hover`
in `WorkflowsPage.css:268-270`.

**Acceptance:**

- 5.2.1 — `.agent-def-card:hover` sets `border-color: var(--text-muted);` (or
  an equivalent border-purpose token if introduced). file:
  `web/src/components/agents/agents.css`.

### 5.3 Migrate empty-state.css to design tokens [category: refactor]
`kind: deliverable`

Target: `web/src/components/chat/styles/empty-state.css:14-35`

`.mobile-chat-drawer-empty`, `.command-palette-empty`, `.activity-tab-empty`,
and `.chat-scope-empty` still use `calc(var(--font-size-base) * 0.85)` for
font-size. The companion `.chat-empty-state__*` classes already migrated to
`var(--text-base)` / `var(--text-xl)` etc. Migrate the remaining four to
`var(--text-sm)` (which is the design system equivalent of "0.85× base").

**Acceptance:**

- 5.3.1 — All four classes (`mobile-chat-drawer-empty`, `command-palette-empty`,
  `activity-tab-empty`, `chat-scope-empty`) use `font-size: var(--text-sm);`
  (or another semantic token) instead of `calc(...)`. file:
  `web/src/components/chat/styles/empty-state.css`.

### 5.4 Use color-mix in CodeGraphExplorer for hardcoded rgba [category: refactor]
`kind: deliverable`

Target: `web/src/components/code-graph/CodeGraphExplorer.css:243`

`.code-graph-detail-sig` uses `background: rgba(0, 0, 0, 0.3)` while neighbor
overlays in the same file (lines 138, 154) use
`color-mix(in srgb, black <pct>%, transparent)`. Switch for consistency.

```css
.code-graph-detail-sig {
  background: color-mix(in srgb, black 30%, transparent);
}
```

**Acceptance:**

- 5.4.1 — `.code-graph-detail-sig` uses `color-mix(in srgb, black 30%,
  transparent)` for `background`. file:
  `web/src/components/code-graph/CodeGraphExplorer.css`.

### 5.5 Verify and remove redundant SessionsPage theme overrides [category: refactor]
`kind: deliverable`

Target: `web/src/components/sessions/SessionsPage.css:1-41`;
verify against `web/src/styles/index.css` token definitions

The `[data-theme="light"]` block at lines 2-41 reapplies the same tokens that
the base styles use (`--color-success-soft`, `--color-error-soft`,
`--color-warning-soft`, `--color-agent`, `--color-warning-foreground`,
`--color-success-foreground`, `--color-error`, `--text-muted`, `--color-info`).
If those tokens ARE theme-aware in `index.css`, the override is dead code.

Verify each token via `grep -n "<token>" web/src/styles/index.css` (or wherever
the theme is defined). For every token confirmed theme-aware, drop the
matching `[data-theme="light"]` rule. For any token that is dark-theme-only,
keep the rule and add a comment explaining why.

**Acceptance:**

- 5.5.1 — Token-by-token audit comment block at the top of
  `SessionsPage.css` documents which tokens were verified theme-aware and
  which (if any) needed light-theme overrides. file:
  `web/src/components/sessions/SessionsPage.css`.
- 5.5.2 — The `[data-theme="light"]` block is reduced to only the rules
  whose underlying tokens are NOT already theme-aware (or removed entirely
  if all tokens are theme-aware). file:
  `web/src/components/sessions/SessionsPage.css`.

### 5.6 Fix mismatched workflows-card-type--agent token pairing [category: refactor]
`kind: deliverable`

Target: `web/src/components/workflows/WorkflowsPage.css:355-358`

`.workflows-card-type--agent` uses `--color-info-soft` background but
`--color-success-foreground` text — the bg is "info" but the text is
"success". Either change to `--color-info` text (info+info pair) or to the
agent-token pair (which exists per `--color-agent` references elsewhere).
Pick the agent pair to match the badge's semantics:

```css
.workflows-card-type--agent {
  background: color-mix(in srgb, var(--color-agent) 15%, transparent);
  color: var(--color-agent);
}
```

If `--color-agent-soft` exists as a separate token, use that for background
and keep `color: var(--color-agent)`.

**Acceptance:**

- 5.6.1 — `.workflows-card-type--agent` background and color reference the
  same semantic family (`--color-agent` or its soft variant), matching the
  pairing pattern of `.workflows-card-type--skill`. file:
  `web/src/components/workflows/WorkflowsPage.css`.

## V1 Verification
`kind: verification`

After all P1–P5 deliverables land:

1. `uv run ruff check src/ && uv run ruff format --check src/` — clean.
2. `uv run mypy src/gobby/mcp_proxy/tools/merge.py
   src/gobby/mcp_proxy/tools/merge_landscape.py
   src/gobby/worktrees/merge/resolver.py` — type-clean.
3. `uv run pytest
   tests/mcp_proxy/tools/test_merge_landscape.py
   tests/worktrees/merge/test_resolver_content_flow.py
   tests/integration/test_merge_orchestrator.py
   tests/agents/test_plan_adversary_self_check.py
   tests/servers/routes/admin/test_token_timeseries_route.py -v` — all pass,
   including the new `test_route_pattern_only_matches_members`.
4. `cd web && npm run build && npm run lint` — green. Visual smoke checks:
   - The `.stop-button` glyph is visible against the red background in dark
     theme.
   - Pipeline approve/reject button labels are readable.
   - Pinned task-memory cards render with translucent warning tint (not
     full opacity from the broken syntax).
   - Config danger-button hover, restart banner, and overridden-prompt
     badge render with intended translucent backgrounds.
5. Spot-render the affected nano-banana skill files in a markdown viewer to
   confirm bias removal and contradictory-constraint fix read cleanly.
6. `gemini-image-gen` agent loads without "model not found" error from the
   Gemini API.

## X1 Rejected suggestions (false positives or intentional design)
`kind: framing`

These suggestions are NOT applied. Reasons recorded so the next reviewer
doesn't relitigate:

- **`.gobby/plans/task-12725-compile-manifest-driven.md:442-443`** — the
  trailing space inside `[TEST] ` / `[IMPL] ` / `[REF] ` IS the prefix.
  Tests assert `task["title"] == f"[IMPL] {entry.title}"`. Removing the
  space from the doc would make the doc inaccurate vs. the implementation.
- **`docs/contracts/plan-coverage.md:92-108`** — the `~~~markdown` outer
  fence is intentional. The wrapped block contains triple-backtick fences
  (the YAML example). CommonMark requires the outer fence to be a different
  character or longer than any inner fence. Switching to backticks would
  break the rendering.
- **`src/gobby/install/shared/skills/nano-banana/references/j-poses.md:11-27`**
  — the `j-poses` recipe is explicitly themed after Japanese gravure
  photography. The language matches the artistic intent the user added.
  Sanitizing the prose would defeat what the recipe exists for. Defer to
  user discretion if broader-audience phrasing is wanted.
- **`src/gobby/mcp_proxy/tools/merge_landscape.py:360-366`** — `verify_in_worktree`
  exists *to* run arbitrary verify commands from task descriptions. Switching
  to `create_subprocess_exec` would require argv-list inputs and break the
  documented "default to whatever the project conventionally runs (`uv run
  pytest`, `npm test`, etc.)" UX in `merge-expert/SKILL.md:36`. The trust
  boundary is the MCP layer; anything with MCP access can already spawn
  agents that edit code.
- **`tests/agents/test_plan_adversary_manifest.py:27-30`** — `Path(__file__).resolve().parents[2]`
  is conventional in this repo (used in `test_plan_adversary_self_check.py:30`,
  `test_merge_orchestrator.py:246`). Switching tests to `importlib.resources`
  is over-engineering and inconsistent with neighbors.
- **`tests/mcp_proxy/tools/test_merge_landscape.py:303-349`** — the verify
  tests use POSIX universals (`echo`, `exit`, `sleep`) that work on every
  dev machine. They run in milliseconds. They're testing subprocess wiring
  directly — the point is the wiring, not what the wiring runs. Mocking the
  subprocess would test the mock, not the tool.
- **`web/src/components/ProjectSelector.tsx:103`** — the `bg-accent/15
  text-accent` selected-state pattern is project-wide (`chat-scope-btn.active`
  in `input.css:115-120`, `agent-picker-item--active` in
  `LaunchAgentModal.css:582`, `reporting-stat-chip.active` in
  `PipelinesPage.css:595`). Patching one component breaks visual consistency.
  Contrast is a design-token-level concern (adjust `--accent` luminance or
  add `--accent-on-soft`); not a single-component patch.

Plan artifact: `.gobby/plans/code-review-skill-friction.md`
**Plan ID:** code-review-skill-friction

# Code-Review Skill Friction Cleanup

## Overview
`kind: framing`

The first interactive trial of the bundled `code-review` skill (ocr delegate mode, range
af85a1c9a4..HEAD) produced three findings and five friction points. The findings went to
session #12778. This plan removes the friction so the next review runs without gate
rejections, hidden diff hunks, or a noisy recall column. The user decided: keep the schema
gate and fix the prose; rank-and-cap recall instead of strict tag filtering; the
"gobby skill as router plus reference library" idea gets its own planning pass (D1).

Facts established during planning:

- The diff truncation comes from `rtk` 0.48.0 (homebrew) rewriting `git diff`/`git show`
  in the Bash tool. Truncated output prints its own recovery line,
  `[full diff: rtk git diff --no-compact]`, and per-hunk markers like
  `... (134 additions truncated)`.
- `get_skill_file` and `get_skill_files` are not in `DISCOVERY_TOOLS`
  (`src/gobby/workflows/enforcement/blocking.py:19-30`), so the
  `require-current-context-schema-before-call` rule demands `get_tool_schema` before the
  first call. The proxy instructions (`src/gobby/mcp_proxy/instructions.py:39`, built from
  `src/gobby/install/shared/prompts/mcp/progressive-discovery.md:32` with an embedded
  fallback copy at `instructions.py:29-39`) and the loading-skills skill line 66 tell agents
  to call it directly.
- `REQUIRED SKILL: <name>` is a prose-only convention (`writing-skills/SKILL.md:93-95`);
  tests assert the literal line.
- `recall_review_context` appends `language` and `repo` as free-text query terms only
  (`src/gobby/review_learning/service.py:568-598`), runs up to 4 searches x 5 hits per
  finding (`_search_recall_matches`, `service.py:404-446`), and has no per-finding cap.
  Lessons carry `lang:<slugify(language)>` and `repo:<slugify(repo, hashed=True)>` tags
  (`src/gobby/review_learning/lessons.py:442-444`).
- Bundled content under `src/gobby/install/shared/` requires regenerating
  `src/gobby/install/bundled_content_manifest.json` via
  `uv run python -m gobby.install.manifest --write`; the commit gate demands it.
- Found work: `tests/skills/test_review_learning_skill.py` line 264 still reads
  `code-reviewer/SKILL.md`, which #22166 replaced with `code-review/`.

## Constraints
`kind: framing`

- Enforcement stays as is: no change to `DISCOVERY_TOOLS`, rule YAML, or the code-index
  rules. Only prose and the skill text change in P1.
- Recall keeps its search count (4 per finding); `tests/review_learning/test_recall_limits.py`
  pins `len(search_calls) == MAX_RECALL_FINDINGS * 4` and must keep passing.
- Existing `reason` strings (`matched review lesson`, `matched project memory`) are asserted
  by tests and stay unchanged.
- No new config knobs. The per-finding cap is a module constant.
- Skill edits happen under a claimed task; the manifest is regenerated in the same commit.

## P1: Instruction surfaces
`kind: framing`

**Goal**: The code-review workflow and the skill-loading prose describe the environment
truthfully: truncated diffs get rerun, `code-index` is loaded up front, and skill reference
loads fetch their schema first.

### 1.1 Update the code-review skill text [category: docs]
`kind: deliverable`

Targets:
- `src/gobby/install/shared/skills/code-review/SKILL.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated from the shared tree
- `tests/skills/test_code_review_skill.py::test_code_review_skill_declares_required_skills`
- `tests/skills/test_code_review_skill.py::test_code_review_skill_handles_truncated_diffs`
- `tests/skills/test_review_learning_skill.py::*` — scope-reason: fix the stale code-reviewer path at line 264

Edit `SKILL.md` (line numbers from HEAD):

1. Line 22, after `REQUIRED SKILL: review-learning.`, add a second line
   `REQUIRED SKILL: code-index.` Keep the review-learning line byte-identical (a test
   asserts it). Add one sentence under it: "Load `code-index` before the first
   `gcode` or file read; the code-index rules block raw reads until it is loaded."
2. Step 3 (line 81, `### 3. Diffs`): after the command table add:

   ```markdown
   Diff output can arrive compacted by a shell wrapper (`rtk` on machines with it
   installed). A hunk marker such as `... (N additions truncated)` or a trailing
   `[full diff: rtk git diff --no-compact]` line means hunks are missing: rerun the
   printed full-diff command for that path before judging it. A path whose diff stays
   truncated is marked `skipped` with that reason, never `reviewed`.
   ```
3. Review-Learning Hooks (recall bullet): append "Pass `language` (and `repo` when
   known); matching lessons rank first in the result."
4. Topic Index (line 170): insert before the bullets: "Both references load through
   `get_skill_file`, which is behind the schema gate. Call
   `get_tool_schema(server_name="gobby-skills", tool_name="get_skill_file")` once per
   context before the first load; the lease then covers later loads."

Then regenerate the manifest:

```bash
uv run python -m gobby.install.manifest --write
```

Add `tests/skills/test_code_review_skill.py` following the existing skill-content test pattern
(read the SKILL.md, assert literal phrases): both `REQUIRED SKILL:` lines, the phrase
`rerun the printed full-diff command`, and `get_tool_schema(server_name="gobby-skills", tool_name="get_skill_file")`.

Found work in the same task: change `tests/skills/test_review_learning_skill.py` line 264 from
`code-reviewer/SKILL.md` to `code-review/SKILL.md` so the test reads the skill that exists.

**Acceptance:**

- 1.1.1 - The skill declares both required skills and the truncated-diff rerun rule. file: `src/gobby/install/shared/skills/code-review/SKILL.md`.
- 1.1.2 - The skill-content test pins the four added phrases. test: `tests/skills/test_code_review_skill.py::test_code_review_skill_declares_required_skills`.
- 1.1.3 - The review-learning skill test reads `code-review/SKILL.md` and passes. test: `tests/skills/test_review_learning_skill.py`.
- 1.1.4 - The bundled manifest matches the shared tree. test: `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`.

### 1.2 Say that skill reference loads fetch their schema first [category: docs] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/prompts/mcp/progressive-discovery.md`
- `src/gobby/mcp_proxy/instructions.py::*` — scope-reason: embedded fallback copy of the progressive-discovery prose
- `src/gobby/install/shared/skills/loading-skills/SKILL.md`
- `docs/guides/mcp-tools.md`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerated from the shared tree
- `tests/mcp_proxy/test_instructions.py::*` — scope-reason: adds the schema-first assertion test
- `tests/skills/test_loading_skills_content.py::*` — scope-reason: adds the schema-first assertion test

Add the same sentence to each surface, right after the sentence that lists the exempt
bootstrap tools (`progressive-discovery.md:22`, the embedded copy at
`instructions.py:29`, the loading-skills skill near line 51, `docs/guides/mcp-tools.md` line 62):

```markdown
`get_skill_file` and `get_skill_files` are not bootstrap tools: call
`get_tool_schema(server_name="gobby-skills", tool_name="get_skill_file")` (or
`get_skill_files`) once per context before the first reference load; the lease then
covers later loads until context is cleared or compacted.
```

Keep the existing "using the exact `get_skill_file(...)` call" wording
(`progressive-discovery.md:32`, `instructions.py:39`, the loading-skills skill line 66) and
prefix it with "after the schema lease above,". `instructions.py` reads the prompt file at
runtime (`build_gobby_instructions`, line 91-101) and carries the fallback copy inline, so
both copies change identically.

Regenerate the manifest (same command as 1.1). Extend the two existing content tests with
one assertion each for the phrase `are not bootstrap tools`.

**Acceptance:**

- 1.2.1 - Both proxy instruction copies name the gated skill-file tools. file: `src/gobby/install/shared/prompts/mcp/progressive-discovery.md`.
- 1.2.2 - The built instructions contain the schema-first sentence. test: `tests/mcp_proxy/test_instructions.py::test_instructions_name_gated_skill_file_tools`.
- 1.2.3 - The loading-skills skill states the same rule. test: `tests/skills/test_loading_skills_content.py::test_loading_skills_states_schema_first_reference_loads`.
- 1.2.4 - The user guide's exempt-tools sentence carries the same clause. file: `docs/guides/mcp-tools.md`.

## P2: Recall ranking
`kind: framing`

**Goal**: `recall_review_context` returns a short, relevant match list per finding, with
language- and repo-matched lessons first, using the searches it already runs.

### 2.1 Rank and cap recall matches per finding [category: code]
`kind: deliverable`

Targets:
- `src/gobby/review_learning/service.py::*` — scope-reason: adds rank_recall_matches and MAX_RECALL_MATCHES_PER_FINDING beside recall_context
- `src/gobby/mcp_proxy/tools/review_learning.py::*` — scope-reason: schema description text for language, repo, and findings only
- `tests/review_learning/test_recall_context.py::*` — scope-reason: adds the ranking and cap tests named in acceptance

Implementation domain: backend.

In `service.py` add a constant next to the existing ones (line 51-53):

```python
MAX_RECALL_MATCHES_PER_FINDING = 5
```

Add a pure helper (module level, near `build_recall_queries`):

```python
def rank_recall_matches(
    matches: list[dict[str, Any]],
    *,
    language: str | None,
    repo: str | None,
) -> list[dict[str, Any]]:
    """Order lessons tagged for this language or repo first, other lessons next,
    project memories last; keep backend order inside each tier; cap the result."""
    wanted = set()
    if language:
        wanted.add(f"lang:{slugify(language)}")
    if repo:
        wanted.add(f"repo:{slugify(repo, hashed=True)}")

    def tier(match: dict[str, Any]) -> int:
        tags = set(match.get("tags") or [])
        if "review-lesson" not in tags:
            return 2
        return 0 if wanted & tags else 1

    ranked = sorted(matches, key=tier)  # sorted() is stable
    return ranked[:MAX_RECALL_MATCHES_PER_FINDING]
```

`slugify` is imported from `gobby.review_learning.lessons` (same slug rules that wrote
the tags, `lessons.py:168` and `:442-444`). In `recall_context` (line 138-180) call
`matches = rank_recall_matches(matches, language=language, repo=repo)` after
`_search_recall_matches` returns and before `grouped.append`. `_search_recall_matches`,
`build_recall_queries`, the fail-open branch, and `MAX_RECALL_FLAT_MATCHES` stay as they
are. No new response fields.

In `src/gobby/mcp_proxy/tools/review_learning.py` update the `language` and `repo`
schema descriptions to "also ranks lessons tagged for this language first" /
"...for this repository first", and state the per-finding cap in the `findings`
description.

Tests in `tests/review_learning/test_recall_context.py`, using the existing
`FakeMemoryManager` and `_service` fixtures (`test_recall_returns_ordinary_and_review_lesson_memories`
at line 163 is the pattern): seed a project memory, a lesson tagged `lang:typescript`,
and a lesson tagged `lang:rust`; call with `language="rust"`; assert the rust lesson is
first, the typescript lesson second, the project memory last. Second test: seed more
than five hits and assert `len(findings[0]["matches"]) == MAX_RECALL_MATCHES_PER_FINDING`
and that the flat `matches` list reflects the capped groups.

**Acceptance:**

- 2.1.1 - A pure ranking helper exists with the stable tiering rule. symbol: `gobby.review_learning.service.rank_recall_matches`.
- 2.1.2 - Language- and repo-tagged lessons come first for a matching call. test: `tests/review_learning/test_recall_context.py::test_recall_ranks_language_and_repo_matched_lessons_first`.
- 2.1.3 - Each finding returns at most `MAX_RECALL_MATCHES_PER_FINDING` matches. test: `tests/review_learning/test_recall_context.py::test_recall_caps_matches_per_finding`.

## Out of scope: gobby skill as router plus reference library
`kind: framing`

Task #22167 (`needs-planning`) carries this direction: keep the on-disk gobby skill a
thin router and add a reference library covering Gobby's features, with task
references inside gobby instead of a separate tasks skill. It restructures the bundled
skills and needs its own planning pass. Agents will then rely on `get_skill_file` for
most loads, which is why 1.2 documents the schema-first rule now.

## Verification
`kind: framing`

```bash
DATABASE_URL="${DATABASE_URL:-postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test}" GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_code_review_skill.py tests/skills/test_review_learning_skill.py tests/skills/test_loading_skills_content.py tests/mcp_proxy/test_instructions.py tests/review_learning/test_recall_context.py tests/review_learning/test_recall_limits.py tests/test_build_backend.py -q
uv run python -m gobby.install.manifest          # verifies manifest against HEAD
uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/
```

End to end, after `gobby restart` (announce to active sessions first; #12261 asked):
`get_skill(name="code-review")` shows both required-skill lines and the truncation rule;
`get_tool_schema` then `get_skill_file` loads a reference without a gate rejection;
`recall_review_context` with `language="rust"` on the three findings from this trial
returns at most five matches per finding with the TypeScript lesson ranked above the
Python hub memories.

## Task Mapping
`kind: framing`

| Plan Item | Task Ref | Status |
|-----------|----------|--------|

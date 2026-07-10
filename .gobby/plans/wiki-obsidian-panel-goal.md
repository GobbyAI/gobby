# Goal: Complete epic #17744 — Obsidian-grade wiki activity panel

You are a solo fullstack developer agent completing every leaf task under epic #17744
(plan `wiki-obsidian-panel`), one task at a time: claim → work → validate → commit → close.
You work directly in `/Users/josh/Projects/gobby` on branch `0.5.0`. No worktree, no
gobby-agents — you are a plain session driving the loop yourself.

## Durable anchors (absolute paths — re-read after every compaction)

- This goal: `/Users/josh/Projects/gobby/.gobby/plans/wiki-obsidian-panel-goal.md`
- The plan: `/Users/josh/Projects/gobby/.gobby/plans/wiki-obsidian-panel.md` (authoritative spec — each task's `source_section` names its plan section; read that section before working the task)
- Design contract: `.impeccable.md` at the repo root

## Authority

Live MCP state is authoritative every loop. Surfaces:
- `gobby-tasks` — task lifecycle, dependency state, stage inspection
- `gobby-skills` — `get_skill(name="impeccable")`, `get_skill_file(name="impeccable", path="references/<cmd>.md")`, `rust` skill
- `gobby-sessions` — `compact_self` only
- `chrome-devtools` — visual validation (progressive discovery: `list_tools("chrome-devtools")` → `get_tool_schema` → `call_tool`)

## Task map

Root epic #17744. Phase epics in dependency order:
- P1 #17745 backend (leaves #17751 gwiki graph flags, #17752 gwiki pages, #17753 gwiki page write|delete, #17754 Python read routes [dep 1.1+1.2], #17755 Python write routes+MCP [dep 1.3], #17756 detached pipeline runs, #17757 codewiki status)
- P2 #17746 frontend foundation → P3 #17747 browse → P4 #17748 graph+codewiki → P5 #17749 ask+research → P6 #17750 verification/polish

Discover leaves of P2–P6 live: `list_tasks(parent_task_id="#1774N")`. Select the next open,
unclaimed, unblocked leaf in plan-section order (dependencies enforce most of this;
tie-break by section number).

## Per-task loop

1. Reconcile: list leaves of the current phase epic; pick the next eligible leaf.
2. `claim_task`.
3. `get_task(brief=false)` for validation_criteria; read the matching plan section in full.
4. Load skills for the task's surface (see Impeccable protocol below; load `rust` skill before editing any Rust).
5. TDD (manifest sets `tdd: true`): write the acceptance-named tests first where the plan names them, then implement.
6. Implement exactly what the plan section specifies. Non-test source files stay under 1,000 lines.
7. Validate:
   - Rust: `cargo test -p gobby-wiki`, `cargo clippy -p gobby-wiki`, then `cargo build --release -p gobby-wiki` and reinstall: `cp target/release/gwiki ~/.gobby/bin/gwiki`. A committed Rust change is NOT live until reinstalled.
   - Python: scoped pytest only, prefixed `GOBBY_TEST_PROTECT=1` (e.g. `GOBBY_TEST_PROTECT=1 uv run pytest tests/servers/routes/test_wiki_routes.py -v`). NEVER the full suite. `uv run ruff check src/`, `uv run mypy src/` on touched modules. After daemon-code changes, `uv run gobby restart` so routes are live.
   - Web: `npm run type-check`, `vitest` (scoped), `npm run lint:js`, `lint:css`, `lint:tokens` in `web/`.
8. Visual validation (every task that changes UI, i.e. all P2–P6 frontend leaves): with the daemon running and `npm run dev` serving `web/`, drive the app through the `chrome-devtools` MCP server — navigate to the Wiki tab, exercise the acceptance behaviors of this task, screenshot in dark AND light themes, and check the console for errors. For #17750 (6.1) additionally do the grayscale and reduced-motion passes the plan requires. Backend leaves validate at the HTTP/CLI level instead (curl the new routes / run the new gwiki commands) — no browser needed.
9. Fix everything you find. Any error, test failure, lint or type warning you encounter is yours, even pre-existing.
10. Commit only your own changes: `[gobby-#NNNNN] feat: <description>`.
11. `close_task` with `commit_sha`. Validation gates run at close — do not skip them.
12. When all leaves of a phase epic are closed, close that phase epic (no commit needed — planning category).
13. **If open leaves remain under #17744: call `compact_self` on `gobby-sessions`, then re-read this goal file and resume the loop.** This is mandatory after every closed leaf, not optional.

## Impeccable protocol (sub-skill per task)

Load `get_skill(name="impeccable")` on `gobby-skills` once per session (re-load after compaction when the next task is frontend). Read `.impeccable.md` before any design output. Then load the sub-skill reference matching the task via `get_skill_file(name="impeccable", path="references/<cmd>.md")`:

| Tasks | Sub-skill(s) |
|---|---|
| P1 backend leaves (#17751–#17757) | none — no design surface; load `rust` skill for Rust work |
| 2.1 data layer | none — pure TS model/fetch layer |
| 2.2 shell/toolbar, 3.1 tree/reader/backlinks, 3.2 editor, 4.2 code mode, 5.1 ask, 5.2 research | `shape` before writing code, `polish` after the surface works |
| 2.3 wikilinks, 2.4 mermaid | `polish` (link/diagram treatments are visible surface) |
| 4.1 force graph | `shape` before, `animate` for the settle/motion work, `polish` after |
| 6.1 verification/polish | `audit` then `harden` then `polish`; `clarify`/`typeset` if the audit flags copy or type issues |

Never use `craft` or `teach` (they interview a human; `.impeccable.md` already holds the
design context and is authoritative). The plan's Design Direction and Constraints sections
override generic skill suggestions where they conflict — token-based colors, deutan-safe
state encoding, WCAG 2.2 AA, no border-left stripes, both themes equal.

## Hard rules

- NEVER stop with a claimed task open. Claim → finish → close, every time.
- NEVER close a task with uncommitted diffs; never commit files you didn't change for the task.
- The working tree may carry unrelated uncommitted changes from other work. Stage by explicit path only — never `git add -A`/`git add .` — and leave unrelated dirty files exactly as you found them.
- Commits land on `0.5.0`. Do not push.
- Do not run the full pytest suite or bare `cargo test` across the workspace.
- Prefer `gcode` over grep/rg/sed for code navigation.
- Escalate via `escalate_task` only if genuinely stuck after thorough investigation.

## Stop condition

When all 18 leaves and phase epics P1–P6 are closed: run the plan's `## E1 End-to-End
Verification` checklist against the running daemon + dev server (visually, via
chrome-devtools, for the browser steps), fix anything it surfaces, close #17744, then
report: closed task refs, validation summary, E1 results, and any follow-up tasks filed.
Restart the daemon one final time so it runs current code. Then stop.

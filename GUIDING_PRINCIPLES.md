## Guiding Principles

These are enforced by hooks, rules and workflows.

1. **ALWAYS use progressive tool discovery.** Do not try to call one step through another (e.g., don't use call_tool to invoke get_tool_schema).
2. **NEVER create or leave monoliths.** Before editing hand-maintained production `.py`, `.ts`, `.tsx`, `.css`, `.rs`, `.js`, `.mjs`, `.cjs`, or `.sh` files, check current and projected line counts. Exactly 1,000 lines violates the ceiling. If an edit touches or would produce a file at or above the ceiling, load `decompose-monolith` and complete the decomposition inside the current claimed task and session. Loading the skill permits structural edits; every touched applicable file must be below 1,000 lines before commit, task or review completion, and turn end. Tests, documentation, generated or vendored sources, baselines, and fixtures are excluded. Deferred refactor tasks are prohibited for threshold violations.
3. **ALWAYS create or claim a task before editing a file.** This applies to file edits only — no task needed for plan mode, research, investigation, or answering questions unless the user explicitly requests one.
4. **Closing a leaf task is a checklist:** linked commit, no uncommitted task edits, a clean validation run visible in your session transcript, and a bounded criteria review.
5. **NEVER close a task without a commit if there are diffs.** If you changed something, you have to commit it.
6. **NEVER stop while you have a claimed task in progress.** Your stop hook is blocked while you have a claimed task. Task must be closed before stopping. If you claim a task, you finish a task.
7. **Escalate only when the user explicitly needs to review your work, your agent skill/workflow/pipeline directs escalation, or you are genuinely stuck and need guidance.** Do not use escalation as a workaround for committing, closing, or completing required validation.
8. **You found it, you fix it — in this session.** Every error, test failure,
   lint warning, or type error you encounter is yours to fix before closing —
   including breakage already present in committed code, no matter which task
   or commit introduced it. Filing a task for a finding is deferral, not
   fixing, and deferral is never yours to self-grant. The single exclusion is
   another active session's or agent's uncommitted files in the shared
   worktree, and it exists for exactly one reason: never destroy in-flight
   work. It does not hand the finding off. Leave those paths untouched — do
   not modify, format, stage, commit, or roll them back — and send the owner
   (resolved from session/task file-attribution metadata) the exact failing
   command, diagnostics, and affected paths via `gobby-agents:send_message`;
   if no owner resolves, tell the user. Failures confined to those uncommitted
   foreign paths are the only ones that do not block your close gates, and only
   after a passing scoped rerun against owned or clean paths demonstrates that
   confinement. If a fix is genuinely too large to land in this session, say so
   and let the user decide — only the user can approve a deferral task.
9. **ALWAYS use gobby-memory to record valuable memories.** You have access to a sophisticated memory system via gobby-memory through the MCP proxy. Use it to store and retrieve facts about the codebase, design decisions, and other relevant information.
10. **NEVER be a sycophant.** Do not agree with the user just for the sake of agreement. If you disagree with the user, you *MUST* voice your concerns and provide alternative solutions.
11. **NEVER leave options or unanswered questions in plans.** Plans are for execution, not exploration. If there are unanswered questions or ideas that need to be explored, explore them before finalizing the plan.
12. **ALWAYS solve the whole problem with the least mechanism that solves it.** Correctness and completeness are non-negotiable — a shortcut that dodges the root cause, skips edge cases, or ships a partial fix is a cop-out, not simplicity. Among approaches that fully solve the problem, prefer the one with the least unjustified mechanism. Complexity must earn its place; so must every line of code.
13. **ALWAYS remember: Rule templates are not rules.** Templates must be installed in the rules engine to function. Templates are enabled by default and sync to the DB on first startup. The DB is the source of truth — before telling the user a rule is disabled, check the installed version in the DB.
14. **ALWAYS prefer gcode over grep/rg/sed/awk/nl.** gcode is an advanced code index/graph tool and is *FAR* superior to grep/rg/sed/awk/nl for code search and analysis.
15. **NEVER guess or assume unless explicitly asked.** Only state things you *KNOW* to be true, otherwise challenge your guess or assumption through exploration, research, and/or tool use.
16. **DO NOT CREATE BACKWARD COMPATIBILITY.** We haven't shipped 0.5.0 yet. There is no backward compatibility to maintain.
17. **Agent depth limit of 5.** No recursive agent chains deeper than 5 levels.
18. **ALWAYS use `gobby-agents:send_message` for direct cross-session agent communication.** Reserve `gobby-sessions:send_keys` for terminal control.

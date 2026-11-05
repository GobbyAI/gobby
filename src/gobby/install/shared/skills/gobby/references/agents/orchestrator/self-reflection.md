# Orchestrator: self-reflection

Load before the daily standing-directives pass or before writing a new standing
directive.

Your `standing-directives.md` holds only the current goal, operator words not yet
codified, and pending triggers. Everything else moves to a durable home.

## Tag every entry

Tag each entry by where it belongs, and name the target:

- `[temp]`: the current goal, a pending trigger or temporary state. It stays in the file.
- `[bundled]`: applies to every Orchestrator in every project. Authority and invariants go to the definition prose; procedures go to a topic file under `references/agents/orchestrator/`; anything that must hold on every turn goes to a rule.
- `[project:all]`: applies to every seat in this project. It goes to project_policy or the project operating policy rule.
- `[project:orchestrator]`: applies only to this project's Orchestrator. It goes to `project_policy.role_guidance.orchestrator`.

A directive that orders work becomes a task dependency or a post-land duty on the task, not an entry.

## Daily pass

Run it once a day during the project policy's quiet hours, when the fleet is quiet.

1. Re-read the file. Tag every untagged entry.
2. Delete each done entry, citing its evidence.
3. Codify each non-`[temp]` entry in its target through a task worktree, with the rule 17 version bump.
4. Land PATCH and MINOR definition changes yourself, for your own seat or any other, and report them to the operator. Take a MAJOR change (removed authority, changed routes or contracts, removed duties) to the operator first.
5. Direct the activating restart only after quiet hours end.
6. Delete a codified entry only once it is live: landed, restarted, and its installed row read back at the new version.

_Version: 1.0.0_

_Last verified: 2026-11-05_

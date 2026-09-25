# Memory

Load when prior project knowledge could change the work, or when capturing,
reviewing, or maintaining durable knowledge. Use `gobby-memory` for persistent
agent memory; never read or write provider-native memory files.

At turn start, before `spawn_agent`, on task claim, and on handoff resume,
Gobby may push a `<memory-index>` of ranked one-line hits. Each line ends in a
`when:` clause drawn from the memory's rationale. Fetch any hit whose clause
matches your situation with `get_memory` before acting, even when the code is
familiar: the payoff is usually a prior decision or observed runtime behavior.

Beyond the index, search the task subject before editing claimed work, before
unfamiliar subsystem work, before characterizing provider or runtime behavior,
and before capture. Most turns and completed tasks need no memory write.
Memory is for facts, preferences, relationships, and finalized rationale that
would take meaningful work to rediscover in an unrelated future session. Write
the rationale as the `when:` clause a future session would match.

Discover tool names with `list_tools` only when unknown; fetch a known tool's
schema before its first unleased call. Pass caller identity through the proxy's
outer `session_id`. Schemas own parameters; guidance does not authorize unrelated
mutations.

| Topic | Load when |
| --- | --- |
| [search](search.md) | Recalling or navigating knowledge |
| [capture](capture.md) | Creating, updating, or replacing durable knowledge |
| [scope](scope.md) | Resolving project ownership or global visibility |
| [maintenance](maintenance.md) | Repairing indices, backing up, or restoring |
| [post-task](post-task.md) | Reviewing memories after a worked leaf closes |
| [dream](dream.md) | Starting or diagnosing memory hygiene runs |

Use tasks for actionable work, plans/evidence for proposals and findings, code
and tests for current behavior, and Git for completed changes. Fix found bugs in
the current task under the repository's found-work ladder; do not store them as
memories or create another task merely to defer them.

On an oversized successful response, use its `result_id` with
`gobby-results:get_tool_result` or `search_tool_result`; do not repeat a mutation
to get smaller output. Missing results require checking scope and services before
concluding that knowledge is absent.

Guide: [Memory system](../../../../../../../../docs/guides/memory.md).

_Last verified: 2026-09-18_

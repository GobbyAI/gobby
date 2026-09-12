# Memory

Load when prior project knowledge could change the work, or when capturing,
reviewing, or maintaining durable knowledge. Use `gobby-memory` for persistent
agent memory; never read or write provider-native memory files.

Search the task subject before editing claimed work, before unfamiliar subsystem
work, and before capture. Most turns and completed tasks need no memory write.
Memory is for facts, preferences, relationships, and finalized rationale that
would take meaningful work to rediscover in an unrelated future session.

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
| [review-lessons](review-lessons.md) | Triaging findings or recording verified review outcomes |
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

_Last verified: 2026-09-12_

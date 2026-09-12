# Review memories after closing work

Load after the post-close prompt for a worked leaf. Use
`gobby-memory:review_task_memories(task_id, changes_summary)` with a concrete
summary of completed behavior and the proxy's caller identity. The task must be
closed in that session or a spawned descendant and belong to the caller project.
Do not invent closure state or mark tracking variables manually.

Read each returned candidate against the completed work. Retain valid entries,
update stale content with a fresh rationale, or delete obsolete entries. Record
new knowledge only when durable value warrants it; most tasks need no write.
Use the returned `source_task_id` for any justified new memory. Zero candidates is
a complete review. The tool records candidate retrieval and closure review state;
it does not itself decide whether the candidates need maintenance.

The installed closure queue and review rules request one bounded review before
turn end or handoff. `pending_reviews_complete` and `pending_reviews` describe
remaining closures. A queued item can remain stored after review: acknowledgement
and queue cleanup are distinct. Do not repeatedly call review just to empty the
queue. Feedback review uses a separate context-epoch latch.

For `task_not_closed`, complete the task workflow first. For foreign closure or
missing identity, resolve the actual caller/owner; do not impersonate another
session. If search fails, preserve that failure and recover memory availability
before claiming review success.

Guide: [Post-task review](../../../../../../../../docs/guides/memory.md#post-task-review)
and [Lifecycle rules](../../../../../../../../docs/guides/memory.md#lifecycle-rules).

_Last verified: 2026-09-12_

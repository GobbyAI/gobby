# Review

Load when reviewing an implemented epic, checking task evidence, or working with
Gobby feedback batches. This overview is a menu: it executes no review, changes
no task state, and does not load any topic. Lease each tool's current schema
before calling it and finish every reference page before acting.

| Topic | Load for | Invocation |
| --- | --- | --- |
| Epic | Launch or perform a whole-epic review | `$gobby review references epic` |
| Evidence | Establish scope, implementation and validation proof | `$gobby review references evidence` |
| Feedback | Capture observations or review a frozen batch | `$gobby review references feedback` |
| Outcomes | Record verdicts and inspect accepted feedback actions | `$gobby review references outcomes` |

Discover feedback evidence through `gobby-feedback`, task records through
`gobby-tasks`, and stage decisions through `gobby-tasks-ops`. General code-review
methodology belongs to the standalone `code-review` skill, and plan review to
[plan](../plan/overview.md).

`$gobby review <epic-ref>` selects an epic review, independent of `gobby build`.
Resolve the epic and interactive/delegated mode before dispatch. Capability help
and topic menus never launch the `review` pipeline. Feedback review is a separate
workflow; an epic verdict does not submit a feedback batch.

The [test-quality guide](../../../../../../../../docs/guides/test-quality.md)
explains local audit evidence. CLI and HTTP feedback review triggers are operator
surfaces; the assigned reviewer uses MCP to read and submit its frozen batch.

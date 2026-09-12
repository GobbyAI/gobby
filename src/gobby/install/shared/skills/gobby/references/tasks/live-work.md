# Live interactive work

Load completely before starting, resuming, or finishing an interactive umbrella
task or changing its `live-session` label. Inspect `claimed_tasks`, then read
each task through `gobby-tasks:get_task`.

Only a root interactive terminal session may change this authorization label;
spawned, automated, and web-chat sessions cannot. The label does **not** exempt
open tasks from turn-end gates. Current rules require completed work or a genuine
durable wait; message text, a label, and a stale wait marker do not create one.

For a requested live-work scope, refuse mixed ordinary/live claims. Reuse the
single matching open umbrella task when owned by this session; claim an unclaimed
match, coordinate if another active session owns it, or create one when no match
exists. Use a scope-specific title, `category="code"`,
`implementation_domain="fullstack"`, `labels=["live-session"]`, `claim=true`,
and concrete criteria covering the touched behavior and verification commands.
Set `allow_automation=false` and `isolation="none"` through `update_task`; confirm
`unattended=false` and the returned settings before editing. Preserve the exact
`gobby:references/tasks/live-work.md` requirement in session `additional_skills`
so context resets reload the operating guidance.

Keep all rounds and checkpoint commits attributed to this umbrella task. Continue
authorized work while gates remain armed. Use the applicable event-driven wait
when waiting on actual dependent work or a review; do not create a wait merely
to end a turn.

When the user finishes the live scope, require exactly one live umbrella claim,
resolve unfinished findings, validate after final edits, and commit changed work.
Use an existing final checkpoint when it already contains all work; never create
an empty commit. Follow the closing topic, including background review waits and
post-close memory review. Remove this extra instruction requirement only after
the task closes.

Expired-session recovery releases clean abandoned live claims and escalates dirty
or indeterminate ones with attribution evidence. On resume, fetch task state
again; an old session variable does not prove ownership.

Guide: [Live Interactive Work](../../../../../../../../docs/guides/tasks.md#live-interactive-work).

_Last verified: 2026-09-12_

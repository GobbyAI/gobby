# Import annotations into Gobby tasks

After [managed MCP attachment](gobby-mcp.md), sync the bundled `annotate` skill
through normal skill sync and inspect its installed row. Invoke:

```text
/gobby annotate /path/to/gobby-annotate-<export-id>.zip
```

An extracted capture directory or export UUID already in the root also works.
The skill stages external captures without overwriting existing files, reads
notes and screenshots through Gobby's managed proxy, inspects project context,
and creates actionable tasks. It does not claim them, start implementation,
or enable automatic dispatch. `/gobby bridge` still handles Drawbridge.

A single actionable annotation becomes a standalone task. Multiple actionable
annotations become tasks under an epic identified by batch ID. Dependencies
are recorded where work must happen in order. Descriptions preserve the original
note, page/target/frame context, viewport geometry, durable file location, export
ID, annotation ID, and revision. Screenshots are viewed through MCP, not embedded
as base64 in task text.

Task creation includes stable `annotate-note:<batchId>:<annotationId>` and
`annotate-revision:<revision>` labels. Epic creation includes
`annotate-batch:<batchId>`. These labels are written in the initial create call,
so retrying after a lost response finds the task. Imports search both open and
closed tasks in the current project.

| Scenario                                     | Expected result                                                                |
| -------------------------------------------- | ------------------------------------------------------------------------------ |
| One new actionable note                      | One unclaimed standalone task                                                  |
| Several new actionable notes                 | One batch epic and one task per actionable note                                |
| Same export imported twice                   | Existing references returned; no duplicates                                    |
| New export, unchanged note IDs/revisions     | Existing tasks reused                                                          |
| Changed revision                             | Show existing task and changed note for direction; no silent rewrite           |
| Failure after some task creations            | Keep tasks and captures; resume missing notes by labels                        |
| Earlier single task, later multi-note export | Reuse that task reference; create epic/new children without silently moving it |
| Non-actionable or ambiguous note             | Summarize or ask for clarification; do not invent implementation               |

Validation of this workflow must use isolated task state and an isolated daemon,
including reconnect/restart and interrupted imports. A source skill file alone
does not prove the installed skill or managed connection works.

# Droid provider contract captures

`command-outcomes-0.174.0.json` records an isolated live `droid exec`
`stream-json` run with zero and exit-7 `Execute` calls. Droid emits the
structured `isError` boolean on both `tool_result` records.

The same probe configured project-local `PostToolUse` and
`PostToolUseFailure` hooks. Droid emitted `PostToolUse` for the successful
command. It emitted neither hook for the nonzero command. Terminal-hook output
therefore uses the documented success-event contract and leaves missing failure
events untouched; managed web-chat uses the live structured `isError` field.

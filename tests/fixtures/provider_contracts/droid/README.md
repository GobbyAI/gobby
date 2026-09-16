# Droid provider contract captures

`command-outcomes-0.174.0.json` records an isolated live `droid exec`
`stream-json` run with zero and exit-7 `Execute` calls. Droid emits the
structured `isError` boolean on both `tool_result` records.

The same probe configured project-local `PostToolUse` and
`PostToolUseFailure` hooks. Droid emitted `PostToolUse` for the successful
command. It emitted neither hook for the nonzero command. Terminal-hook output
therefore uses the documented success-event contract and leaves missing failure
events untouched; managed web-chat uses the live structured `isError` field.

## 0.219.0 hook and session captures

`hook-payloads-0.219.0.jsonl` holds hook stdin that Droid `0.219.0` sent during
`droid exec` runs launched through `spawn_agent`. A capture command placed ahead
of Gobby's `ghook` command in the project hook settings recorded it. There is one
record each for `SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`, and
`SessionEnd`, plus a `PreToolUse` for the `Skill` tool. Session ids and paths are
sanitized.

`session-0.219.0.json` is a structural extract of a later `spawn_agent` run's
transcript. It keeps Gobby's recorded `PreToolUse` denial for `Skill` and the
tool result Droid showed the model. `settings` carries the sidecar's cumulative
`tokenUsage` and `lastCallTokenUsage`. For this live session the daemon recorded
`context_used_tokens` of 21213. `capture_notes` lists what was dropped or cut.

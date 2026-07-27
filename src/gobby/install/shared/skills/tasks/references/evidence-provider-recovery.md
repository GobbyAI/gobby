# Verification Evidence and Provider Recovery

Load this reference when a validation command did not produce trusted completion
evidence.

## Evidence Contract

Run each validation command as one native terminal invocation and retain the
structured terminal result. An intermediate `running`, `yielded`, or textual
excerpt does not establish success or failure.

Native outcome sources:

- Claude Code and Qwen: terminal `PostToolUse` or `PostToolUseFailure`.
- Droid and Grok: structured hook or stream completion.
- Codex: direct command result, or the full chain through `functions.exec`, a
  yielded cell, PTY session ID, and `write_stdin` polling.

## Recovery

- Claude Code or Qwen: rerun as one shell tool call and wait for the terminal hook.
- Droid or Grok: rerun through the native terminal and retain structured completion.
- Codex: place the literal command in the nested `exec_command`, emit the complete
  result, and follow every `wait` or `write_stdin` token until termination.

Machine-derived shell outcomes cannot be replaced by output excerpts or human
summaries. Manual `validation_command` evidence is prohibited.

## Manual Review Evidence

Use manual evidence only for a non-command artifact such as diff review or PR
state:

```python
call_tool("gobby-sessions", "record_verification_evidence", {
    "summary": "Reviewed the final diff against the task acceptance criteria.",
    "evidence_type": "manual_diff_review",
    "supports": "completion readiness for #42",
    "task_id": "#42",
    "command": "manual diff review",
    "scope": "touched lifecycle files"
}, session_id="#2333")
```

If a validation command failed, rerun it successfully after fixing the cause.
Manual review cannot clear that failure.

# Terminal diagnostics and control

Load when inspecting a terminal prompt or sending deliberate terminal input.
Fetch `gobby-sessions:capture_output` or `send_keys` schemas. For cross-session
messages, use `gobby-agents:send_message` instead.

Capture once to inspect a permission prompt, trust dialog, or stalled terminal.
Managed terminal runtimes are preferred, with tmux and then transcript-tail
fallback for capture. Inspect `via` and truncation metadata when present: a
transcript tail is not proof of a live terminal screen. Missing pane/transcript
errors require restoring the actual runtime or using transcript diagnostics.

`send_keys` requires current caller context. Autonomous agent-run sessions cannot
use it. Targets must be the caller, in its project, or in its ancestor/descendant
tree. These checks do not grant permission to answer a user's approval dialog.
Use the session target intentionally; do not use raw tmux to evade a refusal.

Literal input defaults to true. Trailing newlines request one Enter after paste;
nonliteral input selects named keys such as `Enter` or `C-c`. A `/fast` first
token is refused: ask the user to run the provider speed toggle. Managed writes
can be indeterminate; inspect the resulting state before risking duplicate input.

Use `capture_baseline_dirty_files` only for the intended session baseline capture
workflow. It records dirty-file state for edit detection; do not recapture to hide
changes or bypass task evidence. For waiting on output, load the waits topic and
use the run-based primitive rather than repeating snapshots.

Guide: [Terminal tools](../../../../../../../../docs/guides/sessions.md#terminal-tools).

_Last verified: 2026-09-12_

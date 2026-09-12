# Live variable values

Load before reading or writing session values. Use top-level `get_variable` and
`set_variable`; these do not need a discovery lease. Pass the intended session
explicitly. Omit read `name` for the effective map; inspect `exists` as well as
`value` to distinguish missing from present null.

Read first, change only an authorized user setting, then read it back. Runtime
set accepts native JSON values and coerces boolean/null/number strings; it does
not parse array/object strings or evaluate rule expressions. `session_task`
resolves in the target session's project. Lists of required instructions must be
arrays of non-empty identifiers. Never forge tracking or remove obligations.

Reserved-name errors require the owning lifecycle operation, not CLI/HTTP bypass.
Public read/write sequences are not atomic increments. CLI get/set are operator
local-DB commands with scalar parsing and session scope only. See
[live mutation](../../../../../../../../docs/guides/variables.md#mutation) and [runtime ownership](../../../../../../../../docs/guides/variables.md#auto-managed-variables).

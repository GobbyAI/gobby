# Variable overrides

Load when customizing defaults or persisting a user definition. Read the original
row and live value, identify the intended scope, then change the user-owned layer.
Keep bundled definitions Gobby-owned; do not edit them through permissive APIs.

Project user variables live in `.gobby/workflows/variables/`; global user variables
in `~/.gobby/workflows/variables/`. Use a single named `type: variable` document with
`variable` and `value`, not the bundled grouped format. Import supplies scope and
can add a project override without replacing its global row. Agent-specific
variables use agent-definition tooling (`update_agent_variables`, schema first).

Explicit export and auto-export now use the importable named format. Verify the
file; development mode or missing destination skips auto-export, and errors can
follow a successful DB write. Older grouped auto-export files need re-export or
conversion. Reload through the shared workflow reload operation only when the
configuration change is authorized, and inspect errors and installed rows after it.
See [defaults and overrides](../../../../../../../../docs/guides/variables.md#defaults-and-overrides). Test mutations using
isolated fixtures, not the user's live daemon or state.

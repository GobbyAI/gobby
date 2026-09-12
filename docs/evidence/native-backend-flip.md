# Native backend default-flip evidence

`tmux` remains the checked-in default. The native default flip is authorized only
after the P7 host-driven acceptance suite is green in ordinary CI on macOS and Linux
at the same commit, with no later red row.

Append one row per qualifying CI run in execution order, earliest first. Use `macOS`
or `Linux` for the OS and `green` or `red` for the result. A red row invalidates every
earlier green pair; a later same-commit macOS/Linux green pair establishes new
qualifying evidence.

Status: **no qualifying run**.

| Date | OS | Commit | Workflow run | Result |
| --- | --- | --- | --- | --- |

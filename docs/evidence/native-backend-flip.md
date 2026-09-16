# Native backend default-flip evidence

`native` is the checked-in default as of 2026-09-16. The flip was authorized by the
rows below: the P7 host-driven acceptance suite (`terminal-acceptance`) green in
ordinary CI for every required OS at one commit, with no later red row for a
required OS.

Append one row per qualifying CI run in execution order, earliest first. Use `macOS`
or `Linux` for the OS and `green` or `red` for the result. A red row for a required OS
invalidates every earlier green row; a later green row for every required OS at one
commit establishes new qualifying evidence.

**Linux deferral (user decision, 2026-09-16).** The required OS set is macOS only.
Linux rows stay recorded when a run produces them, but a missing or red Linux row
neither blocks the flip nor invalidates macOS evidence. The gate reinstates Linux when
the user says so; at that point `_REQUIRED_OSES` in
`tests/config/test_native_backend_flip.py` regains `linux` and the checker again
demands a same-commit macOS/Linux green pair.

Status: **qualifying run recorded** — `dbd8a48c51` on `0.5.0`, macOS green (Linux
green as well), no later red row. That commit carries the #22293 (machine-executed
Guard set G) and #22295 (strict snapshot modes) fixes, and the rows come from the
`terminal-acceptance` job added by #22101. The #22102 prerequisite — green P7
host-driven acceptance recorded here before the flip — remains mandatory. Other jobs
in that run are red for unrelated pre-existing reasons tracked in #22423; they are not
`terminal-acceptance` rows and do not affect this gate.

| Date | OS | Commit | Workflow run | Result |
| --- | --- | --- | --- | --- |
| 2026-09-16 | macOS | `dbd8a48c51` | https://github.com/GobbyAI/gobby/actions/runs/35086965128 | green |
| 2026-09-16 | Linux | `dbd8a48c51` | https://github.com/GobbyAI/gobby/actions/runs/35086965128 | green |

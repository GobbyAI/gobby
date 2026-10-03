# Task #23259: Remove cship from the Claude status line chain

Completed machine configuration change on 2026-10-01 by gobby#15009, under
Orchestrator direction. Josh confirmed deletion with button value
`23259-delete-yes`, relayed by the Assistant and confirmed by the Orchestrator.

## Live settings

Changed only `statusLine.command` in `/Users/josh/.claude/settings.json`:

Before:

```text
GOBBY_STATUSLINE_DOWNSTREAM='cship' /Users/josh/.gobby/bin/ghook --gobby-owned --cli=claude --type=statusline
```

After:

```text
/Users/josh/.gobby/bin/ghook --gobby-owned --cli=claude --type=statusline
```

The update replaced that JSON string directly, preserving all other bytes and
the original file mode. JSON parsing and equality after changing only that key
passed. Concurrent-content checks preceded atomic replacement.

After this verification, the Orchestrator reported that Josh was running
`/statusline` to create a PS1-derived downstream behind ghook. At his direction,
live settings edits stopped. The ghook-only state above records the completed
removal; Josh's later downstream is his subsequent change. Acceptance criterion
1 was amended to require no cship downstream, allowing that later user change.

The exact live command ran with `{}` as stdin: exit 0, empty stdout and stderr.
The inherited environment contained no `GOBBY_STATUSLINE_DOWNSTREAM`. A temporary
`cship` executable placed first on PATH would record invocation; its marker was
absent after rendering. Source confirmation: `crates/ghook/src/statusline.rs`
`handle` reads that environment variable; `handle_with` forwards only when it is
present and nonempty. With no downstream ghook currently emits no visible text.

## Stored backups

Removed only the `statusLine` key from 32 files matching
`/Users/josh/.claude/settings*` that retained cship. This includes plain cship,
legacy Python wrappers, nested wrappers, timestamped backups, the live-ghook-test
backup, and `settings.json.bak-statusline`. All other JSON keys and original modes
were preserved. Every settings file was then checked for absence of `cship`.

[Machine evidence](task-23259-cship-removal.json) names all 32 paths and records
before/after hashes and preservation assertions without exposing private settings.

Installer source stores rollback copies beside settings as
`settings.json.<timestamp>.backup`. Reinstall derives its downstream exclusively
from the current `statusLine`; uninstall restores only the downstream extracted
from that current command. Historical copies are used on write failure, rather
than discovered as an independent downstream registry. No separate status line
backup registry was found in the installer. A read-only scan of Gobby hooks,
settings, and personal files found cship mentions only in processed hook inbox
history, which is not installer restoration state and was preserved.

## Confirmed deletions

All paths were inspected before deletion; none was a symlink:

| Path | Type | Size before deletion |
| --- | --- | ---: |
| `/Users/josh/.local/bin/cship` | Regular file | 2,873,280 bytes |
| `/Users/josh/.config/cship.toml` | Regular file | 947 bytes |
| `/Users/josh/.config/cship/` | Directory | 96 bytes |
| `/Users/josh/.config/cship/sample-context.json` | Regular file | 1,087 bytes |

The directory contained exactly `sample-context.json`, a cship sample context
with the expected Claude context keys. Its exact contents were checked again
before deletion. All four entries are now absent. The JSON evidence records
file hashes. No unrelated configuration or login state was changed.

## Validation

Run from the isolated task worktree based on 0.5.0
`a96d41f2ea9017f8b98bb0ccd2d86b035cff8f24`:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/cli/installers/test_claude_statusline.py -q --tb=short
```

Result: **17 passed**. Relevant existing tests:

- `TestConfigureStatusline.test_sets_statusline_when_none`
- `TestExtractDownstream.test_returns_none_without_env_var`
- `TestRestoreStatusline.test_removes_when_no_downstream`

An additional protected check used a temporary home and project, patched
`Path.home`, and mocked unrelated global hook/content/router/MCP/trust installers.
The real `install_claude` settings pipeline ran twice with the cleaned command;
both results succeeded with no downstream and preserved a sentinel settings key.
The real `uninstall_claude` then succeeded, removed `statusLine`, preserved the
sentinel, and left no cship reference in temporary settings or backups.

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run python /tmp/gobby-23259-verify.py
```

Result: live render, both isolated reinstalls, and isolated uninstall passed.
The verification script remains at that machine-local path for peer review.
No live `gobby install`, uninstall, daemon restart, or binary promotion ran.

```sh
gcode grep -F 'cship' src/ -m 50
```

Result: exit 0, no matches. No installer or bundled template under `src/` writes
cship. Production code and tests required no edits: existing installer behavior
already supports removing a downstream through the settings correction.

Source review and actual task-close admission remain with the designated peer
reviewer and Lane Manager.

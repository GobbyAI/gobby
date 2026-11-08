# Handoff continuation proof

The host execution gate runs `tests/e2e/test_handoff_continuation_e2e.py` on the
exact reviewed SHA after independent review and security approval. It covers
Claude, Codex, Grok, AGY and Droid, each with compact, clear and compact during a
restart hold. Developers run the related non-E2E checks before review.

The seat stages its own handoff in its launch turn. The test then requires its
native SessionStart hook on the isolated daemon, the daemon's continuation prompt
and the continuation turn's Stop event. Compact cases also require durable
compact-boundary evidence. No operator write is admitted after launch. The
restart case holds compact submission with a test-only socket barrier, verifies
the isolated restart request returns `409 handoff_pending`, and releases the
barrier. There is no provider sleep or production test seam.

Only Gobby state is isolated. The daemon has temporary HOME, GOBBY_HOME, database
schema, ports and terminal sockets. Each provider process receives the operator's
normal HOME and existing provider configuration locations. The fixture neither
installs nor modifies provider configurations, and never opens or links provider
credentials. Use the normal existing logins. A missing executable or login is a
provider-named failure after admission, never a skip or a passing case.

`GOBBY_DAEMON_URL` and GOBBY_HOME pin native hooks and MCP clients to the isolated
daemon. The launcher removes the parent seat's identity and preserves the host's
new terminal identity. Codex also receives the production runtime overrides for
its rebuilt MCP and shell environments. The CLI routing regression invokes real
`ghook` for all five provider event formats against two disposable HTTP receivers;
only the isolated receiver may receive a hook. The live test separately requires
each seat's native SessionStart on its isolated WebSocket connection.

## Admission and host execution

Set `GOBBY_HANDOFF_PROOF_EXECUTION=PD_AUTHORIZED_ISOLATED_EXECUTION` and point
`GOBBY_HANDOFF_PROOF_ADMISSION` at an owner-private JSON admission file. Without
the execution grant, the live module is excluded from ordinary checks. Once
admitted, missing prerequisites fail. The admission file contains only:

- `grant`, `reviewed_commit` and `reviewed_tree` (full Git identities).
- `issued_at` (timezone-aware, within five minutes of batch admission).
- `exclusion_message_id`, `complete_exclusion_map: true` and
  `excluded_identities` (the current complete exclusion map, at least 26 UUIDs).
- `native`: gcode, gdaemon, ghook, gterm and gclient; `providers`: claude, codex,
  grok, agy and droid; and `support_tools` for required supporting executables.
  Each executable has an absolute `path` and its installed-byte `sha256`.
- `artifact_dir` (owner-private) and `worktree_test_notice_id` (the announced
  worktree test exception).

Provider entries have no auth-reference field. Do not provision special login
folders. Run with the isolated test database and protection enabled:

```sh
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/e2e/test_handoff_continuation_e2e.py -v --no-cov
```

For every provider/scenario, retain the artifact's exact source identity,
isolated endpoint, hook registration, terminal identity, automatic write trace,
continuation result and cleanup result. The host owner additionally verifies
that the running daemon acquired no test session, hook event or agent run. That
production-side observation is a host runtime responsibility; the developer's
CLI routing regression is not a substitute for it.

Before landing, the host owner demonstrates RED on `3dc780c716` or an equivalent
revert of `7231f91990`, then GREEN with the fix on the reviewed candidate. Refresh
the admission for each source identity. Record the exact-SHA passing E2E evidence
only after every case passes. Landing and closure gates enforce that evidence for
the continuation paths declared in YAML. Retain failed isolated state when
cleanup cannot prove terminal ownership and shutdown.

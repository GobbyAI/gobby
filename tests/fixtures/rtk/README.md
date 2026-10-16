# RTK close-gate contracts

`pytest-assertion-raw.txt` is deterministic pytest-shaped input, not a recorded
execution of the sample test. `pytest-assertion-0.51.0.txt` records output
of the installed RTK 0.51.0 filter, with a trailing newline added by the repository's
end-of-file fixer:

```sh
rtk pipe -f pytest < pytest-assertion-raw.txt > pytest-assertion-0.51.0.txt
```

The transcript tests use this output for compact RED attribution. They also cover
literal recall and `recall --full`, with IDs linked uniquely to the original
failed run. Recalled text retains the original run's chronology and must arrive
before implementation to earn RED credit. Existing recall regressions cover
wrong IDs, duplicate IDs, failed retrieval, oversized output and pipes.

## Close-gate inventory

The checklist in `src/gobby/mcp_proxy/tools/tasks/_lifecycle_close.py` has these
command-output consumers:

| Gate | Input and parity coverage |
| --- | --- |
| 10: validation commands | Native transcript pass/fail outcomes; pytest coverage, Vitest related paths, type audit and criterion command credit. The matrix covers bare, hook, explicit RTK, proxy and bypass forms. |
| 12: TDD evidence | Compact assertion and stub failures, failing test names, recalled output, RED/GREEN chronology and passing test coverage. |
| 13: criteria review | Receives Gate 10/12 details and successful-command operational markers. The deterministic inputs above and operational marker parity are tested; the paid review itself is not replayed as a unit test. |

Gates 1–9 read task/session/repository state, child state, criteria, change summary,
linked commits, task scope and uncommitted edits. Gate 11 resolves named acceptance
artifacts from linked source. They do not parse shell validation output or runner
exit codes. Git inspection uses its own subprocess results, outside the transcript
RTK command/output path.

## RTK 1.0.1 compatibility review

Pinned upstream sources:

- [CLI and rewrite contract](https://github.com/rtk-ai/rtk/blob/v1.0.1/src/lib.rs):
  `proxy` is a silent alias of `bypass`; rewrite 0/3 returns a rewrite, 1 falls
  back, and 2 with a message blocks through the host. Gobby preserves host
  permission checks and package-manager launchers. The version/help probe is
  parameterized through 1.0.1. Pipeline messages are bounded plain text.
- [Recall store and retrieval](https://github.com/rtk-ai/rtk/blob/v1.0.1/src/core/retriever.rs):
  the SQLite gzip store retains the hash/exit/time/truncation fields Gobby reads;
  `--full` retrieves complete output, while default recall can return a suffix.
- [Release](https://github.com/rtk-ai/rtk/releases/tag/v1.0.1): the 1.0 CLI removes
  `--ultra-compact`; Gobby's probe/rewrite invocation does not use that flag.
  Default and Apache OSS distributions have different licensing. Installing or
  upgrading either distribution remains an Orchestrator action.

`contracts-1.0.1.json` contains byte-exact stdout/stderr and exit codes captured
from the official `rtk-aarch64-apple-darwin.tar.gz` release. The archive SHA256 is
`4aae2f60cfc49c9e81b8c8c57020aa0cb824da608c78b4f0cf5c0988e19e824e`, verified
against the published checksums and rechecked after copying into the worktree.
The binary ran with an isolated HOME, project, history database and tee directory.
The JSON strings preserve the absence of a final newline in rewrite/pipe output.

The capture uses `rtk --version`, `rtk rewrite --help`, and
`rtk rewrite -- 'git status'` with isolated `.claude/settings.json` permission
rules: allow returns exit 0, no rules return exit 3, and deny returns exit 2 with
empty stdout. The native `proxy` alias and `bypass` both execute
`printf 'compatibility\n'` unchanged. `rtk pipe -f pytest` filters the deterministic
raw fixture above. A disposable `pytest` emitter on PATH prints that same raw
fixture and exits 1; `rtk pytest tests/test_feature.py -q` compresses and retains
it, and `rtk recall bac15d0b1278 --full` returns the complete original bytes.
A passing emitter prints `1 passed in 0.01s\n` and exits 0. These exercise the
real RTK binary without running tests against user state.

### Upstream help/implementation skew

The 1.0.1 rewrite help advertises an exit-2 message for a pipeline denial, but
the [pinned implementation](https://github.com/rtk-ai/rtk/blob/v1.0.1/src/hooks/rewrite_cmd.rs#L85-L109)
has no stdout-message branch. A valid user pipeline that denies shell calls
still yields exit 3 from `rewrite`. The native `rtk hook claude` entry point
does apply that pipeline and returns a JSON denial with the reason
`Compatibility policy: command refused` (captured as `hook_pipeline_deny`).

Gobby's exit-2 message regression is explicitly **defensive**: it extracts that
native hook reason and feeds it to the advertised rewrite contract. It is not
represented as captured 1.0.1 rewrite output. The oversized-denial test covers
more than 64 KiB, so an output bound cannot silently run a denied command.
The Orchestrator's criterion ruling requires this distinction alongside the
captured 0/3/empty-2 forms and an explicit safe-to-upgrade close statement.
Installed RTK remains 0.51.0; this task never replaces it.

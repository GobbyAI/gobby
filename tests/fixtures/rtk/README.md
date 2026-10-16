# RTK close-gate contracts

`pytest-assertion-raw.txt` is deterministic pytest-shaped input, not a recorded
execution of the sample test. `pytest-assertion-0.51.0.txt` is the unedited output
of the installed RTK 0.51.0 filter:

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

These are source-contract checks and fixtures. Installed RTK remains 0.51.0;
this task does not replace it. Binary 1.0.1 smoke verification, if available, is
reported separately in the task close evidence rather than implied by fixtures.

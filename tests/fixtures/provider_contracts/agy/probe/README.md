# Gate 0 probe helpers

Run-authored helpers behind the `evidence/*.txt` records that a one-line `agy`
command could not capture. Each prints the same sections its evidence file carries,
so `evidence/<record>.txt` is the literal command line followed by the helper's
stdout (scrubbed per plan §1.1). Run them from this directory's parent
(`tests/fixtures/provider_contracts/agy/`) on a machine with `agy` signed in.

| Helper | Record | Invocation |
| --- | --- | --- |
| `inputfmt.py` | 1.1.18 (and the 1.1.4 stream-json image shape) | `python3 probe/inputfmt.py <case> [CONVERSATION_ID]` — cases `eof`, `conv`, `idle`, `cancel`, `shapes`, `shapes2`, `shapes3`, `shapes4` |
| `cancel.sh` | 1.1.8 print half | `bash probe/cancel.sh INT` / `bash probe/cancel.sh TERM` |
| `net.sh` | 1.1.9 print half | `bash probe/net.sh` |
| `net-interactive.sh` | 1.1.9 interactive half | `bash probe/net-interactive.sh 150` (samples the running `agy-gate0` tmux session's agy process for 150 s) |
| `layout.py` | 1.1.22 interactive conversation | `python3 probe/layout.py <CONVERSATION_ID> <needle>...` |

Environment: `GATE0_WORKSPACE` is the throwaway workspace every launch passes as
`--add-dir` and `cwd` (default: a fresh temporary directory); `GATE0_RUNS` is the
directory raw run files land in (default: a fresh temporary directory). Both appear in
raw output as real paths and are scrubbed to `<WORKSPACE>` / `<PROBE_SCRATCH>` before
the output is committed.

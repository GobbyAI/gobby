# SRT violation false positives (2026-10-01)

Root-cause record for #23288 and the later log cleanup. All reads were read-only and sampled.
No full-file scans ran on the large logs except the size/count survey.

## Problem

#23118 (f031d1680b) made the SRT runner record every violation past SRT's 100-entry store
tail. By 2026-10-01, `~/.gobby/logs/sandbox-violations/` held 11.77 GB in 4,064 files, of
which 10.23 GB was in 97 files written since 2026-10-01. No file was still growing at
survey time.

Each entry has the keys `command`, `encodedCommand`, `line` and `timestamp`:

- `command` is the run's launch argv. That's the codex `exec` argv with its inlined prompt
  and config, 54,111 to 56,387 characters, and it is identical in every entry of a run
  except 2 `node --version` probe entries. It is 99.5% of the bytes.
- `encodedCommand` is the same per-run value in encoded form (80 characters).
- `line` is the denied operation: `<process>(<pid>) deny(1) <operation> [<target>]`.
- `timestamp` is ISO-8601 UTC.

Run identity comes from the file name `<agent_run_id>.jsonl`, plus a
`<agent_run_id>.settings.json` policy sidecar. Entries carry no session or run id.

## Tallies

Sampled from the head and tail 2,000 lines of each of the top 5 logs:

| Share | Denial | Source |
| --- | --- | --- |
| 53.7% | `system-info vfs.disk-space` | cc, rustc, cp and codex free-space queries |
| 35.6% | `sysctl-read kern.iossupportversion` | libsystem reads it at every process exec |
| 5.0% | `mach-lookup com.apple.SystemConfiguration.configd` | codex reachability checks (693 of 707) |
| 4.4% | `network-outbound` (bare) | codex 483, python3.14 103, gcode 27, Python 15 |
| rest | file denials | includes true positives: `file-read-metadata ~/.gobby/bootstrap.yaml` (python3.14) and `file-write-mode ~/.gobby/machine_id` |

The first three are harmless reads, and builds succeed under them. Together they are 94.3%
of the sample.

All five top runs are codex:

- 07586586: 15,904 entries, feat-23084-gcore-postgres-pool, a Rust build
- 87f3f247: the #23248 close reviewer
- d150410e: task-23190
- 8e28d50c: task-23154
- 2e8ad12f: main checkout

## SRT references

These refer to sandbox-runtime 0.0.76 at
`~/.gobby/tools/srt/0.0.76/node_modules/@anthropic-ai/sandbox-runtime/dist`.

- `sandbox/macos-sandbox-utils.js` lines 677-736 is the seatbelt profile's sysctl
  allowlist. It has about 60 names, omits `kern.iossupportversion`, and has no
  `system-info` allow.
- `sandbox/sandbox-config.js:788` defines `IgnoreViolationsConfigSchema` as
  `record(string, string[])`, and line 914 makes it the optional `ignoreViolations`
  setting.
- `sandbox/sandbox-violation-store.js:71` is `shouldIgnoreViolation`. A `"*"` pattern
  suppresses a violation when the pattern is a substring of its `line`. Every producer
  shares it: the macOS log monitor, the Linux seccomp observer and proxy denies.

## Fix

`render_srt_settings` in `src/gobby/agents/srt_runtime.py` now emits:

```json
"ignoreViolations": {"*": [
  "sysctl-read kern.iossupportversion",
  "system-info vfs.disk-space",
  "mach-lookup com.apple.SystemConfiguration.configd"
]}
```

The sandbox profile and its enforcement are unchanged; only recording is filtered. There
is no runner change, no `runner_sha256` change and no SRT restage, and the fix goes live
on a normal daemon restart. Josh superseded the per-run cap in 13294a317b, and it is
reverted on the branch.

## Network-outbound classification

Every sampled `network-outbound` denial is bare, for example
`codex(4001) deny(1) network-outbound`. None carries a host, IP or socket path.

All five runs have the same `allowedDomains` with `strictAllowlist`: the OpenAI and
ChatGPT hosts, the package registries, `localhost` and `127.0.0.1`. A domain blocked by
SRT's filtering proxy would appear as a separate proxy-deny entry naming the host, and
none appear. So these are seatbelt kernel denials of connections that bypassed the proxy,
and they are not legitimate domains missing from the allowlist.

The destination cannot be proved from these logs. Direct-IP connects, or unix-socket
connects outside `allowUnixSockets`, are the likely candidates. Per the PD's ruling
(17:55 CDT), they stay recorded as true positives. No domain is added, and no
destination-capture logging is added, under the logging ban.

## Survey scripts

All of these are read-only. They were run with
`uv run --directory <worktree> python <script>`.

### Totals and field shapes

```python
import collections
import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / ".gobby" / "logs" / "sandbox-violations"
files = sorted(ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)
total = sum(p.stat().st_size for p in files)
since = datetime(2026, 10, 1).timestamp()
recent = [p for p in files if p.stat().st_mtime >= since]
print(f"files={len(files)} total_bytes={total} since_10-01={len(recent)} bytes={sum(p.stat().st_size for p in recent)}")
now = time.time()
for path in files[:8]:
    stat = path.stat()
    lines = 0
    field_bytes: collections.Counter[str] = collections.Counter()
    commands: collections.Counter[int] = collections.Counter()
    first_ts = last_ts = None
    keys: set[str] = set()
    with path.open("rb") as handle:
        for raw in handle:
            lines += 1
            entry = json.loads(raw)
            keys.update(entry)
            for key, value in entry.items():
                field_bytes[key] += len(json.dumps(value))
            commands[hash(entry.get("command"))] += 1
            stamp = entry.get("timestamp")
            first_ts = first_ts or stamp
            last_ts = stamp
    print(
        f"{path.stem[:8]} bytes={stat.st_size} lines={lines} age_s={now - stat.st_mtime:.0f} "
        f"distinct_commands={len(commands)} keys={sorted(keys)} "
        f"field_bytes={dict(field_bytes.most_common(4))} first={first_ts} last={last_ts}"
    )
```

### Sampled tallies (head and tail 2,000 lines, top 5)

```python
import collections
import json
import re
from pathlib import Path

ROOT = Path.home() / ".gobby" / "logs" / "sandbox-violations"
SAMPLE = 2000
TAIL_BYTES = 2000 * 70_000
DENY = re.compile(r"(?P<proc>[\w .\-]+)\(\d+\) deny\(\d+\) (?P<op>\S+)(?: (?P<target>.*))?")


def sampled_lines(path: Path) -> list[bytes]:
    with path.open("rb") as handle:
        head = [handle.readline() for _ in range(SAMPLE)]
        size = path.stat().st_size
        handle.seek(max(size - TAIL_BYTES, 0))
        tail = handle.read().splitlines()[1:][-SAMPLE:]
    return [line for line in head + tail if line.strip()]


def category(op: str, target: str) -> str:
    if op.startswith("file-"):
        parts = target.split("/")
        return f"{op} {'/'.join(parts[:5])}"
    return f"{op} {target[:60]}"


files = sorted(ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)[:5]
overall: collections.Counter[str] = collections.Counter()
for path in files:
    counts: collections.Counter[str] = collections.Counter()
    for raw in sampled_lines(path):
        match = DENY.search(json.loads(raw).get("line", ""))
        counts[category(match["op"], match["target"] or "") if match else "unparsed"] += 1
    overall.update(counts)
total = sum(overall.values())
for name, count in overall.most_common(15):
    print(f"  {count / total:6.1%} {count:6d} {name}")
```

### Process attribution for one run

Pass a run-id prefix as the argument.

```python
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path.home() / ".gobby" / "logs" / "sandbox-violations"
path = next(ROOT.glob(f"{sys.argv[1]}*.jsonl"))
DENY = re.compile(r"(?P<proc>[\w .\-]+)\(\d+\) deny\(\d+\) (?P<op>\S+)(?: (?P<target>.*))?")
by_kind: collections.Counter[tuple[str, str, str]] = collections.Counter()
with path.open("rb") as handle:
    for raw in handle:
        line = json.loads(raw).get("line", "")
        match = DENY.search(line)
        if match is None:
            by_kind[("?", "?", line[:80])] += 1
            continue
        by_kind[(match["proc"].strip(), match["op"], (match["target"] or "")[:70])] += 1
for (proc, op, target), count in by_kind.most_common(12):
    print(f"  {count:6d} {proc} | {op} | {target}")
```

### Command field repetition and sidecar keys

```python
import collections
import json
from pathlib import Path

ROOT = Path.home() / ".gobby" / "logs" / "sandbox-violations"
files = sorted(ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)[:3]
for path in files:
    lengths: collections.Counter[int] = collections.Counter()
    with path.open("rb") as handle:
        for _ in range(2000):
            raw = handle.readline()
            if not raw:
                break
            lengths[len(str(json.loads(raw).get("command", "")))] += 1
    print(f"{path.stem[:8]} command_lengths(head 2000)={dict(lengths.most_common(4))}")
    sidecar = path.with_name(f"{path.stem}.settings.json")
    if sidecar.exists():
        print(f"  sidecar_keys={sorted(json.loads(sidecar.read_text()))}")
```

### Network-outbound lines and policy

```python
import collections
import json
from pathlib import Path

ROOT = Path.home() / ".gobby" / "logs" / "sandbox-violations"
files = sorted(ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)[:5]
shown: collections.Counter[str] = collections.Counter()
for path in files:
    network = json.loads(path.with_name(f"{path.stem}.settings.json").read_text()).get("network", {})
    print(f"{path.stem[:8]} allowedDomains={network.get('allowedDomains')}")
    with path.open("rb") as handle:
        for _ in range(2000):
            raw = handle.readline()
            if not raw:
                break
            entry = json.loads(raw)
            line = str(entry.get("line", ""))
            proc = line.split("(", 1)[0]
            if "network-outbound" in line and shown[proc] < 2:
                shown[proc] += 1
                print(f"{path.stem[:8]} {entry.get('timestamp')} {line!r}")
```

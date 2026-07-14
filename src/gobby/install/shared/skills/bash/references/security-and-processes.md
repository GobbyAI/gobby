# Security And Processes

## Keep Code And Data Separate

Never evaluate untrusted text as shell code. Replace dynamic command strings
with arrays and replace dynamic dispatch with a fixed `case` statement or an
allowlisted function map.

Treat `source` as code execution. Source only trusted, ownership-controlled
files. A file that is writable by a lower-trust user, checkout, artifact, or
temporary workspace is an execution boundary.

## Validate For The Sink

Validation depends on how a value is used:

- filenames: preserve bytes, quote, and use `--`;
- identifiers: allowlist the exact grammar;
- URLs: pass as one argument and constrain schemes when security-sensitive;
- regular expressions and globs: keep user data out unless that syntax is the
  explicit interface;
- remote host commands: avoid an extra shell interpretation layer.

Escaping once does not make a value safe for every later interpreter.

## Temporary Resources And Permissions

Use `mktemp` instead of predictable names. Create sensitive files under a
restrictive umask and set explicit permissions where required. Do not follow
untrusted symlinks during privileged writes.

Check ownership and permissions before reading config or credential files in a
privileged context. Use atomic replacement when partial writes would corrupt
state.

## Secrets

Keep secrets out of:

- command-line arguments visible in process listings;
- `set -x` traces;
- stdout, stderr, and log files;
- environment variables inherited by unrelated children;
- temporary files without restrictive permissions;
- shell history and committed fixtures.

Disable tracing before secret handling and test redaction paths. Prefer file
descriptors or purpose-built secret mechanisms supported by the target tool.

## Subprocesses And Concurrency

Capture and check child statuses. Bound parallelism and define cancellation.
Use the platform's locking primitive for shared mutable files and write through
a temporary file followed by an atomic rename when supported.

Avoid parsing `ps` output or using PID existence alone as proof of ownership.
PIDs are reused. Store enough identity to validate the process or use a real
supervisor.

## Privilege And Remote Execution

Keep privileged sections small and explicit. Do not set SUID/SGID on scripts.
Avoid downloading content directly into a shell. Fetch, authenticate or verify,
inspect, and execute as distinct operations under the required trust policy.

Use absolute paths or a controlled PATH in privileged automation. Clear or
allowlist environment variables crossing privilege boundaries.

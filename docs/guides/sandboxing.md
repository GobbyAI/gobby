# Sandbox Configuration

Gobby owns sandbox selection and policy for daemon-managed runtimes. Managed
terminal agents use Anthropic Sandbox Runtime (SRT) by default. Web chat keeps
its provider-native sandbox because those runtimes use SDK, app-server, or ACP
process models rather than the managed terminal launch path.

For the test and lifecycle matrix, see
[sandbox-compatibility.md](./sandbox-compatibility.md).

## Backends

Two explicit backends are supported:

- `srt` wraps the complete provider command in one host-native process-tree
  sandbox before tmux starts. It is the `agent_sandbox` default.
- `provider-native` renders the provider's own sandbox flags. It is the
  `web_chat_sandbox` default and an explicit rollout/debug option for managed
  agents.

Backend selection never falls back. If SRT installation, policy validation, or
preflight fails, the agent run fails before tmux creation. If a provider has no
provider-native renderer, selecting `provider-native` also fails closed.

Operators who cannot run SRT can explicitly roll managed agents back to the
provider sandbox with `agent_sandbox.backend: provider-native`. Daemon startup
warns when the default SRT runtime or its Node.js 20.11+ prerequisite is
unavailable and includes this rollback setting.

## Managed SRT Installation

`gobby install` installs `@anthropic-ai/sandbox-runtime` 0.0.66 under
`~/.gobby/tools/srt/0.0.66` (or the configured `GOBBY_HOME`). Gobby verifies:

- the fixed npm tarball URL and SHA-256 checksum;
- npm's published integrity value;
- a checked-in lockfile and its SHA-256 checksum;
- the installed package name and version;
- the Gobby runner checksum recorded in the installation receipt.

The package is Apache-2.0 licensed and requires Node.js 20.11 or newer. The
installer runs `npm ci` against the checked-in dependency graph with lifecycle
scripts disabled. Launches use absolute paths to the verified Node executable
and Gobby-managed runner; they never run `npx` or resolve an SRT executable from
`PATH` per spawn.

An invalid existing installation is replaced atomically. Installation failure
is fatal to `gobby install`, and selecting SRT remains fail-closed until the
runtime is repaired.

## Configuration

```yaml
web_chat_sandbox:
  enabled: true
  backend: provider-native
  mode: permissive
  allow_network: true
  extra_read_paths: []
  extra_write_paths: []

agent_sandbox:
  enabled: true
  backend: srt
  mode: permissive
  allow_network: false
  extra_read_paths: []
  extra_write_paths: []
  extra_deny_read_paths: []
  extra_deny_write_paths: []
  allowed_domains: []
  denied_domains: []
  allow_git_network: false
  allow_package_registries: false
  allow_unix_sockets: []
```

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Enables the selected backend |
| `backend` | `srt` or `provider-native` | agents: `srt`; web chat: `provider-native` | Host sandbox implementation |
| `mode` | `permissive` or `restrictive` | `permissive` | Provider-native renderer mode; SRT uses its canonical path policy |
| `allow_network` | bool | agents: `false`; web chat: `true` | Provider-native network switch; SRT rejects `true` because unrestricted network is not supported |
| `extra_read_paths` | list of paths | `[]` | Additional readable roots |
| `extra_write_paths` | list of paths | `[]` | Additional writable roots |
| `extra_deny_read_paths` | list of paths | `[]` | Additional hidden roots |
| `extra_deny_write_paths` | list of paths | `[]` | Write-deny exceptions inside writable roots |
| `allowed_domains` | list of domains | `[]` | Additional SRT outbound domains |
| `denied_domains` | list of domains | `[]` | SRT outbound denials; denial wins over allowance |
| `allow_git_network` | bool | `false` | Adds common Git-forge domains for fetch, pull, and push |
| `allow_package_registries` | bool | `false` | Adds common registry domains and local package-cache write roots |
| `allow_unix_sockets` | list of paths | `[]` | Unix socket paths allowed by SRT |

Paths are expanded and canonicalized outside the workspace before policy
generation. Relative operator paths resolve from the workspace. Symlinked
workspace paths resolve to their real target, and linked-worktree Git metadata
from `git rev-parse --git-dir --git-common-dir` is added deliberately so local
commits work.

## Default Filesystem Policy

SRT read access is broad unless denied, so Gobby uses SRT's deny-then-allow
model:

1. Deny the user's home and the configured Gobby home.
2. Re-allow the canonical workspace, declared writable/readable roots, provider
   authentication directories, the selected provider and Node installations,
   Git configuration, and only these Gobby resources: `machine_id`, `bin`,
   `hooks`, and an explicit `GOBBY_PROMPT_FILE`.
3. Allow writes to the workspace, linked Git metadata, the exact Gobby hook
   inbox, explicit extra roots, and package caches only when the package
   capability is enabled. SRT provides its own isolated runtime temporary path.
4. Deny writes to SSH/AWS/GPG/Kubernetes/Google Cloud credential roots and
   `extra_deny_write_paths` after write grants. SRT write denials win over
   overlapping allowances and symlink paths.

Gobby does not grant blanket read or write access to `~/.gobby`. The exact
`hooks/inbox` write exception preserves spool-first lifecycle delivery. Provider
authentication directories are readable because browser/file-based login must
continue to work; API-key environment variables use the credential handling
described below.

## Network And Credentials

The SRT policy is a strict allowlist. It contains:

- the selected provider's model API domains;
- the hostname from an explicit provider API base;
- `localhost` and `127.0.0.1` for Gobby's daemon and WebSocket services;
- operator `allowed_domains`;
- Git-forge or package-registry domains only when their separate capability is
  enabled.

SRT 0.0.66 expresses loopback policy by host, not by destination port. Gobby
records the configured daemon and WebSocket ports in its canonical policy, but
the rendered SRT grant permits the two loopback hosts. Do not treat this release
as exact loopback-port isolation.

Known provider API-key variables are masked inside the sandbox and injected by
SRT only for the provider's API hosts or the configured API-base host. Operator
domains, Git forges, and package registries do not receive those credentials.
SRT's plaintext credential injection fallback is disabled.

Local Git commits need filesystem access only. Network Git operations require
`allow_git_network: true`. Package downloads require
`allow_package_registries: true`; enabling the latter also makes the supported
local package caches writable.

## Launch And Lifecycle

Gobby constructs the complete Claude, Codex, Qwen, Grok, or Droid command first,
preflights SRT, and then wraps that argv exactly once before tmux creation.
Provider-native OS sandbox flags are omitted in SRT mode, while provider approval
policies, tool permissions, MCP/browser/computer-use controls, authentication,
worktree/clone isolation, resource limits, and hooks remain independent.

Hook delivery is fail-open for every tool-use event. No CLI treats `PreToolUse`
as critical, so a PreToolUse denial degrades to allow when the daemon is
unreachable. Session-lifecycle hooks (`session-start` / `SessionStart`,
`session-end` / `SessionEnd`, `pre-compact` / `PreCompact`) still fail closed.
Turn-level `Stop` is never critical.

The Gobby runner inherits stdin/stdout/stderr and forwards `SIGINT`, `SIGTERM`,
`SIGHUP`, and `SIGWINCH` to the provider process. Tmux remains responsible for
detach/reattach, pane capture, resize delivery, provider/PID verification, and
process-group cleanup. The SRT policy explicitly enables pseudo-terminal
operations so host-native confinement does not block the active tmux PTY.
Each launch also gets a mode-`0700` private temporary directory under its
sandbox run directory; Gobby passes it through `CLAUDE_CODE_TMPDIR`, which SRT
maps to the child's `TMPDIR`. Daemon-stop resume regenerates and preflights a
fresh policy before launching the provider resume command.

Generated files live outside the workspace at
`~/.gobby/run/sandbox/<agent-run-id>/`:

- `settings.json` is mode `0600` and its canonical bytes determine the effective
  policy hash.
- `violations.jsonl` is mode `0600` and receives SRT violation events.

Agent-run records expose the backend, enforcement state, SRT version, policy
hash, violation count, and up to the 100 most recent violation events. They do
not expose arbitrary paths supplied through persisted metadata; violation files
are read only when they resolve to a regular, non-symlink file under Gobby's
sandbox run directory.

## Provider-Native Rollout Backend

Explicit `provider-native` agent mode retains the existing renderers for Claude,
Codex, Qwen, and Grok. Droid has no Gobby provider-native renderer, so that
combination is rejected. Web chat continues to use its established SDK,
app-server, ACP, or per-session backend behavior and policy-hash resume checks.

## Security Boundary

SRT uses Seatbelt on macOS and bubblewrap on Linux. It is a pre-1.0 runtime and
reduces host exposure for managed agents, but it is not the future microVM
boundary for hostile repositories. Higher-risk unattended execution remains a
separate microVM follow-up.

_Last verified: 2026-07-21_

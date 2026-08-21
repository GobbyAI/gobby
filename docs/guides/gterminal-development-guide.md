# gterm and gclient Development Guide

Technical internals for developers and agents working in `crates/gterminal`
(`gobby-terminal` → `gterm`) and `crates/gclient` (`gobby-client` → `gclient`).

`gobby` remains the Python daemon and Click CLI. Users run `gclient` for the
workspace TUI. `gterm` is the supervised native PTY host. They are two
binaries on purpose: the Ghostty VT engine sits behind `gobby-terminal`'s
`vt-engine` feature and must not ship inside every client install.

## Build

Zig 0.15 is a **build-time** dependency only for `vt-engine` (the `gterm`
binary and its CI jobs). `gobby-client` never invokes Zig.

```bash
# Host (requires zig 0.15 on PATH)
cargo build --release -p gobby-terminal --features vt-engine

# Workspace client (Zig-free)
cargo build --release -p gobby-client
```

End users receive prebuilt GitHub release assets. The installer local-workspace
fallback for `gterm` builds `--features vt-engine` with a 600s timeout; if
`zig` is missing it skips that step with an explicit reason and continues to
Gobby-hosted GitHub assets. `gclient`'s local build is ordinary cargo.

### Rebuild and reinstall

A crate change is live only after rebuild **and** reinstall via a new inode.
macOS kills processes that exec an in-place-overwritten signed binary:

```bash
cargo build --release -p gobby-terminal --features vt-engine
cargo build --release -p gobby-client
mkdir -p ~/.gobby/bin
cp target/release/gterm ~/.gobby/bin/.gterm.new
mv -f ~/.gobby/bin/.gterm.new ~/.gobby/bin/gterm
cp target/release/gclient ~/.gobby/bin/.gclient.new
mv -f ~/.gobby/bin/.gclient.new ~/.gobby/bin/gclient
chmod 755 ~/.gobby/bin/gterm ~/.gobby/bin/gclient
```

`install -m 755` over an existing path is not sufficient on macOS.

## Protocol contracts

Host sockets are Unix domain, mode 0600, under `~/.gobby/`:

| Socket | Protocol | Credential |
| --- | --- | --- |
| `gterm-control.sock` | JSON-lines control (`spawn`, `kill`, `resize`, `write`, `list`, …) | `~/.gobby/gterm-control.token` (daemon only) |
| `gterm-frames.sock` | bincode frames (`Hello`/`Welcome`, `AttachTerminal`, `Frame`, …) | `~/.gobby/local_cli_token` |

Golden corpus: `crates/gterminal/tests/fixtures/wire_golden/`. `gclient` attaches
frame streams from the host and talks to the daemon only through the public
HTTP/WS API. Writes never go on the frame socket.

Logs: `~/.gobby/logs/gterm.log` (host) and `~/.gobby/logs/gclient.log` (TUI).

## Release tags

Stage-0 ships four triples and no Windows assets:
`aarch64-apple-darwin`, `x86_64-apple-darwin`, `x86_64-unknown-linux-gnu`,
`aarch64-unknown-linux-gnu`.

Tag prefixes: `gterm-v*` and `gclient-v*`. **Publish `gobby-terminal` version
*V* before tagging `gclient-v*` that depends on *V*.** `release-gclient.yml`
preflights crates.io and fails before `cargo package` / `cargo publish` if that
version is unpublished or yanked. Do not invent a combined workflow.

# Host Profile

Build and keep a profile of the machine you serve. It is the layer that differs per
user, so it never ships with Gobby and is never committed.

## Where It Lives

`host-profile.md` in your persistent agent workspace. Read it at the start of every
assignment. Create it when it is missing. Refresh a section when a command shows it
changed, and date each refresh.

## Discovery Commands (No Root)

```bash
uname -a; sw_vers 2>/dev/null; cat /etc/os-release 2>/dev/null
sysctl -n machdep.cpu.brand_string hw.ncpu hw.memsize 2>/dev/null; nproc 2>/dev/null
sysctl -n hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu 2>/dev/null
df -h; diskutil list 2>/dev/null | head -40
command -v cargo rustc sccache cargo-nextest uv node docker podman orb colima macmon
docker info --format '{{.OperatingSystem}} cpus={{.NCPU}} mem={{.MemTotal}}' 2>/dev/null
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null
for v in CARGO_TARGET_DIR CARGO_INCREMENTAL CARGO_BUILD_JOBS RUSTC_WRAPPER SCCACHE_DIR SCCACHE_CACHE_SIZE; do printf '%s=%s\n' "$v" "$(printenv "$v")"; done  # named, nonsecret only; never dump env
ls ~/.gobby/cache 2>/dev/null
```

Project facts come from the repository: build docs (CONTRIBUTING, development guides),
`Cargo.toml` profiles, `.cargo/config.toml`, compose files, CI workflows. Note their
paths in the profile rather than copying their content.

## Template

```markdown
# Host profile: <hostname>
Updated: <date and local time>

## Hardware
- Model, chip, performance/efficiency cores, memory

## OS And Security
- OS version; security tools seen (XProtect, EDR, MDM) and whether managed

## Storage
- Volumes, free space, any dedicated build or scratch volume and its settings
  (indexing off, backup exclusion), and which locations the operator approved for
  experiments

## Toolchains
- Rust (cargo, sccache, nextest), Python (uv), Node; versions

## Containers
- Runtime (Docker Desktop, OrbStack, Colima, Podman), VM CPU and memory
- Running services and which project owns each

## Project Build Facts
- Paths to the project's build docs and config; current build root

## Baselines
- Idle load, build times, security-service CPU, with date and method

## Open Findings
- Finding, status, owner
```

## Rules

- Record what a command showed, with the date. Mark guesses as guesses.
- Never store secrets, tokens, connection strings or personal data.
- An operator's statement about the machine (for example, a volume they set up) goes in
  with "operator-stated" and the date until a command confirms it.

_Version: 1.0.2_
_Last verified: 2026-11-06_

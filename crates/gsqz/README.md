<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img src="https://raw.githubusercontent.com/GobbyAI/gobby/main/logo.png" alt="Gobby" width="160" />
</p>

<h1 align="center">gsqz</h1>

<p align="center">
  <strong>Squeeze your CLI output before it eats your context window.</strong><br>
  YAML-configurable output compressor for LLM token optimization.
</p>

<p align="center">
  <a href="https://github.com/GobbyAI/gobby-cli/actions/workflows/ci.yml"><img src="https://github.com/GobbyAI/gobby-cli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/GobbyAI/gobby-cli/releases/latest"><img src="https://img.shields.io/github/v/release/GobbyAI/gobby-cli" alt="Release"></a>
  <a href="https://github.com/GobbyAI/gobby-cli"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
</p>

---

## The Problem

AI coding assistants run shell commands and dump the full output into the context window. A 500-line `cargo test` run that could be summarized as "78 passed" instead burns thousands of tokens. Multiply that across a session and you're losing real context to noise.

## The Fix

gsqz wraps your shell commands and compresses their output using pattern-matched pipelines. It knows how to summarize `git status`, collapse test output, group lint errors by rule, and truncate walls of text — all configured in plain YAML.

```text
$ gsqz -- cargo test
[Output compressed by gsqz — cargo-test, 95% reduction]
test result: ok. 78 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

## How It Works

```text
command → split compound (&&, ||, ;) → match pipeline regex → apply steps → output
                                              │
              ┌──────────┬──────────┬─────────┼─────────┬──────────┐
              │          │          │         │         │          │
         match_output  filter    replace   group    truncate    dedup
         (short-circuit) (noise) (normalize) (aggregate) (cap)  (collapse)
```

1. **Split** — Compound commands (`&&`, `||`, `;`) are split; last segment gets priority for matching
2. **Match** — First pipeline whose regex matches the command wins
3. **Match Output** — Optional short-circuit: if output matches a success pattern, return a short message immediately
4. **Filter** — Strip lines matching patterns (blank lines, hints, boilerplate)
5. **Replace** — Regex substitution with backreferences for normalizing paths, versions, etc.
6. **Group** — Aggregate by mode: `git_status`, `lint_by_rule`, `errors_warnings`, `by_file`, etc.
7. **Truncate** — Keep head + tail, omit the middle (with per-section support for diffs)
8. **Dedup** — Collapse consecutive identical/near-identical lines

## Installation

### Pre-built binaries

Download from [GitHub Releases](https://github.com/GobbyAI/gobby-cli/releases/latest):

```bash
# macOS (Apple Silicon)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gsqz-aarch64-apple-darwin.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/

# macOS (Intel)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gsqz-x86_64-apple-darwin.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/

# Linux (x86_64)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gsqz-x86_64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/

# Linux (ARM64)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gsqz-aarch64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/
```

### Build from source

```bash
cargo install gobby-squeeze
```

### With Gobby

gsqz is installed automatically as part of the [Gobby](https://github.com/GobbyAI/gobby) platform. If you're using Gobby, you already have it.

## Usage

```bash
# Compress command output (backward-compatible form)
gsqz -- git status
gsqz -- cargo test
gsqz -- npm run lint

# Explicit output subcommand (same behavior)
gsqz output -- cargo build

# Compress prose/text from stdin
echo "In order to fix this..." | gsqz input
echo "verbose text" | gsqz input --level aggressive

# Compound commands — last segment matched for pipeline selection
gsqz -- "cargo build && cargo test"

# Show compression stats
gsqz --stats -- pytest tests/

# Generate default config in current directory
gsqz --init

# Dump resolved config
gsqz --dump-config

# Use a custom config file
gsqz --config my-config.yaml -- make build
```

## Configuration

gsqz uses a single YAML config file:

1. **CLI override** — `gsqz --config path/to/config.yaml`
2. **Local** — `./gsqz.yaml` in the current working directory
3. **Built-in default** — compiled into the binary (fallback)

First found wins entirely — no merging between layers.

On first run, if no `./gsqz.yaml` exists, gsqz automatically creates one from the built-in default so you can start editing immediately.

### Managing config

```bash
# Generate (or regenerate) the default config
gsqz --init

# If gsqz.yaml already exists, --init backs it up first
gsqz --init  # → gsqz.yaml.bak + fresh gsqz.yaml

# Dump the resolved config (human-readable summary)
gsqz --dump-config
```

### Example: Add a custom pipeline

```yaml
# gsqz.yaml
pipelines:
  my-tool:
    match: '\bmy-tool\s+run\b'
    steps:
      - filter_lines:
          patterns:
            - '^\s*$'
            - '^DEBUG:'
      - group_lines:
          mode: errors_warnings
      - truncate:
          head: 15
          tail: 10
      - dedup: {}
```

### Built-in pipelines

| Pipeline | Matches | What it does |
|----------|---------|-------------|
| `git-status` | `git status` | Groups by status code (Modified, Added, Untracked...) |
| `git-diff` | `git diff` | Per-file section truncation |
| `git-log` | `git log` | Head + tail with omission marker |
| `pytest` | `pytest`, `uv run pytest` | Extracts failures + summary |
| `cargo-test` | `cargo test` | Extracts failures + summary |
| `generic-test` | `npm test`, `go test`, etc. | Failure grouping |
| `python-lint` | `ruff`, `mypy`, `pylint` | Groups by rule code |
| `js-lint` | `eslint`, `tsc`, `biome` | Groups by rule code |
| `cargo-build` | `cargo build`, `cargo clippy` | Errors/warnings grouping |

...and 10+ more. Run `gsqz --dump-config` to see the full list.

### Step reference

| Step | Parameters | Description |
|------|-----------|-------------|
| `match_output` | `rules: [{pattern, message, unless?}]` | Short-circuit: return message if output matches pattern |
| `filter_lines` | `patterns: [regex...]` | Remove lines matching any pattern |
| `replace` | `rules: [{pattern, replacement}]` | Regex substitution with backreferences ($1, $2) |
| `group_lines` | `mode: <mode>` | Aggregate lines by mode |
| `truncate` | `head`, `tail`, `per_file_lines`, `file_marker` | Keep head + tail, omit middle |
| `dedup` | (none) | Collapse consecutive similar lines |
| `compress_prose` | `level: lite\|standard\|aggressive` | Prose compression with protected regions |

**Group modes:** `git_status`, `pytest_failures`, `test_failures`, `lint_by_rule`, `by_extension`, `by_directory`, `by_file`, `errors_warnings`

## Integration with AI Agents

### With Gobby (automatic)

[Gobby](https://github.com/GobbyAI/gobby) configures gsqz automatically via its rules engine — every Bash command gets wrapped transparently. No setup needed.

### Standalone (skill-based)

gsqz ships with a [`SKILL.md`](SKILL.md) file that teaches AI agents when and how to use gsqz. Copy it into your agent's instruction set (e.g., `.claude/commands/`, project `CLAUDE.md`, or equivalent) and the agent will prefix verbose commands with `gsqz --`.

## Platform support

| Platform | Architecture | Status |
|----------|-------------|--------|
| macOS | Apple Silicon (aarch64) | Supported |
| macOS | Intel (x86_64) | Supported |
| Linux | x86_64 | Supported |
| Linux | ARM64 (aarch64) | Supported |
| Windows | x86_64 | Supported |
| Windows | ARM64 (aarch64) | Supported |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License

[Apache 2.0](LICENSE) — Use it, fork it, build on it.

---

<p align="center">
  <sub>Part of the <a href="https://github.com/GobbyAI/gobby">Gobby</a> suite.</sub>
</p>

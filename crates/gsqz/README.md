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
  <a href="https://github.com/GobbyAI/gsqz/actions/workflows/ci.yml"><img src="https://github.com/GobbyAI/gsqz/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/GobbyAI/gsqz/releases/latest"><img src="https://img.shields.io/github/v/release/GobbyAI/gsqz" alt="Release"></a>
  <a href="https://github.com/GobbyAI/gsqz"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
</p>

---

## The Problem

AI coding assistants run shell commands and dump the full output into the context window. A 500-line `cargo test` run that could be summarized as "78 passed" instead burns thousands of tokens. Multiply that across a session and you're losing real context to noise.

## The Fix

gsqz wraps your shell commands and compresses their output using pattern-matched pipelines. It knows how to summarize `git status`, collapse test output, group lint errors by rule, and truncate walls of text — all configured in plain YAML.

```
$ gsqz -- cargo test
[Output compressed by gsqz — cargo-test, 95% reduction]
test result: ok. 78 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

## How It Works

```
command → match pipeline regex → apply steps → compressed output
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                 filter          group           truncate
              (remove noise)  (aggregate)     (head + tail)
                                    │
                                  dedup
                            (collapse repeats)
```

1. **Match** — First pipeline whose regex matches the command wins
2. **Filter** — Strip lines matching patterns (blank lines, hints, boilerplate)
3. **Group** — Aggregate by mode: `git_status`, `lint_by_rule`, `errors_warnings`, `by_file`, etc.
4. **Truncate** — Keep head + tail, omit the middle (with per-section support for diffs)
5. **Dedup** — Collapse consecutive identical/near-identical lines

## Installation

### Pre-built binaries

Download from [GitHub Releases](https://github.com/GobbyAI/gsqz/releases/latest):

```bash
# macOS (Apple Silicon)
curl -L https://github.com/GobbyAI/gsqz/releases/latest/download/gsqz-aarch64-apple-darwin.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/

# macOS (Intel)
curl -L https://github.com/GobbyAI/gsqz/releases/latest/download/gsqz-x86_64-apple-darwin.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/

# Linux (x86_64)
curl -L https://github.com/GobbyAI/gsqz/releases/latest/download/gsqz-x86_64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/

# Linux (ARM64)
curl -L https://github.com/GobbyAI/gsqz/releases/latest/download/gsqz-aarch64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gsqz /usr/local/bin/
```

### Build from source

```bash
cargo install --git https://github.com/GobbyAI/gsqz
```

### With Gobby

gsqz is installed automatically as part of the [Gobby](https://github.com/GobbyAI/gobby) platform. If you're using Gobby, you already have it.

## Usage

```bash
# Wrap any command
gsqz -- git status
gsqz -- cargo test
gsqz -- npm run lint

# Show compression stats
gsqz --stats -- pytest tests/

# Dump resolved config
gsqz --dump-config

# Use a custom config file
gsqz --config my-config.yaml -- make build
```

## Configuration

gsqz uses layered YAML configuration:

1. **Built-in** — Ships with 20+ pipelines for common tools
2. **Global** — `~/.gobby/gsqz.yaml` (or `$XDG_CONFIG_HOME/gsqz/config.yaml`)
3. **Project** — `.gobby/gsqz.yaml` (or `.gsqz.yaml`)
4. **CLI override** — `--config path/to/config.yaml`

Later layers override earlier ones. Pipelines merge by name; settings merge by field.

### Example: Add a custom pipeline

```yaml
# .gobby/gsqz.yaml
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
| `filter_lines` | `patterns: [regex...]` | Remove lines matching any pattern |
| `group_lines` | `mode: <mode>` | Aggregate lines by mode |
| `truncate` | `head`, `tail`, `per_file_lines`, `file_marker` | Keep head + tail, omit middle |
| `dedup` | (none) | Collapse consecutive similar lines |

**Group modes:** `git_status`, `pytest_failures`, `test_failures`, `lint_by_rule`, `by_extension`, `by_directory`, `by_file`, `errors_warnings`

## Integration with Claude Code

Add gsqz as a shell wrapper in your Claude Code settings:

```json
{
  "hooks": {
    "bash_tool": {
      "command": "gsqz -- $COMMAND"
    }
  }
}
```

Or with [Gobby](https://github.com/GobbyAI/gobby), this is configured automatically.

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

# Tool Compression

Two compression systems reduce token usage by condensing verbose tool output before it reaches the LLM context window.

| System | What It Compresses | When It Runs |
|--------|-------------------|--------------|
| **Output Compression** | Shell command output (git, pytest, linters, etc.) | CLI wrapper or PostToolUse hook |
| **Code Index Compression** | Large file reads for indexed files | PostToolUse rule on Read |

## Output Compression

### Quick Start

```bash
# Compress command output through the CLI
gobby compress -- pytest tests/

# Show compression statistics
gobby compress --stats -- git diff

# See available options
gobby compress --help
```

### How It Works

```
Command output (raw string)
    ↓
Length check (< min_length? → passthrough)
    ↓
Excluded command check (matches exclusion pattern? → passthrough)
    ↓
Pattern matching (find strategy for command)
    ↓
Primitive pipeline (filter → group → truncate/dedup)
    ↓
Max lines cap (60% head, 40% tail)
    ↓
Validation (empty? < 5% savings? → passthrough)
    ↓
Compressed output
```

The output is prefixed with `[Output compressed by Gobby — STRATEGY, PCT% reduction]` when compression is applied.

### Supported Commands

20 command strategies covering 30+ command patterns:

#### Git

| Strategy | Pattern | Pipeline |
|----------|---------|----------|
| `git-status` | `git status` | filter noise → group by status letter |
| `git-diff` | `git diff` | filter → truncate per file (50 lines) |
| `git-log` | `git log` | filter merge lines → truncate (40 head, 5 tail) |
| `git-transfer` | `git push/pull/fetch/clone` | filter progress → truncate (5+5) |
| `git-mutation` | `git add/commit/stash/tag/branch` | filter → truncate (5+5) |

#### Test Runners

| Strategy | Pattern | Pipeline |
|----------|---------|----------|
| `pytest` | `pytest`, `py.test` | filter passed → extract failures → truncate (50+20) |
| `cargo-test` | `cargo test` | filter passed → extract failures → truncate (50+20) |
| `generic-test` | `npm test`, `vitest`, `jest`, `mocha`, `go test` | filter passed → extract failures → truncate (50+20) |

#### Linters

| Strategy | Pattern | Pipeline |
|----------|---------|----------|
| `python-lint` | `ruff`, `mypy`, `pylint` | dedup → group by rule → truncate (60+10) |
| `js-lint` | `eslint`, `tsc`, `biome`, `oxlint` | dedup → group by rule → truncate (60+10) |
| `go-lint` | `golangci-lint`, `staticcheck` | dedup → group by rule → truncate (60+10) |

#### File Operations

| Strategy | Pattern | Pipeline |
|----------|---------|----------|
| `ls-tree` | `ls`, `tree` | group by extension → truncate (40+10) |
| `find` | `find` | group by directory → truncate (40+10) |
| `grep` | `grep`, `rg`, `ripgrep` | dedup → group by file → truncate (40+10) |

#### Build & Package

| Strategy | Pattern | Pipeline |
|----------|---------|----------|
| `build` | `cargo build`, `go build`, `next build`, `webpack`, `make` | filter progress → group errors/warnings → truncate (30+10) |
| `package-mgmt` | `pip install/list`, `npm install/ls/list`, `uv pip/sync/add` | filter progress → truncate (10+10) |

#### Container & CLI

| Strategy | Pattern | Pipeline |
|----------|---------|----------|
| `docker-list` | `docker ps/images` | truncate (30+5) |
| `container-logs` | `docker logs`, `kubectl logs` | dedup → truncate (30+20) |
| `gh-cli` | `gh pr/issue list/view` | filter → truncate (30+5) |
| `download` | `wget`, `curl` | filter progress → truncate (10+10) |

**Fallback:** Unrecognized commands get `truncate(head=20, tail=20)`.

### Compression Primitives

Four composable primitives that can be chained in any order:

#### `filter_lines(lines, patterns=[])`

Remove lines matching any regex pattern.

```python
# Remove empty lines and git hints
filter_lines(lines, patterns=[r"^\s*$", r"^\s*\(use"])
```

#### `group_lines(lines, mode="")`

Aggregate lines by a grouping key. Supported modes:

| Mode | Behavior | Max Per Group |
|------|----------|---------------|
| `git_status` | Group files by status letter (M/A/D/??) | 20 |
| `pytest_failures` | Extract FAILURES section + short summary | — |
| `test_failures` | Extract FAIL/ERROR lines | — |
| `lint_by_rule` | Group by lint rule code | 5 |
| `by_extension` | Group files by extension | 10 |
| `by_directory` | Group paths by parent directory | 10 |
| `by_file` | Group grep results by file path | 5 |
| `errors_warnings` | Separate errors (max 20) from warnings (max 10) | — |

Groups exceeding the max show `[... and N more]`.

#### `truncate(lines, head=20, tail=10, per_file_lines=0, file_marker="")`

Keep first N + last M lines, replacing the middle with `[... N lines omitted ...]`.

When `per_file_lines` and `file_marker` are set, each section (delimited by the marker regex) is independently truncated.

```python
# Global truncation
truncate(lines, head=40, tail=5)

# Per-file truncation (for git diff, split on @@ hunks)
truncate(lines, per_file_lines=50, file_marker=r"^@@\s")
```

#### `dedup(lines)`

Collapse consecutive lines that are identical or differ only in numbers (progress counters, line numbers, timestamps).

```
Downloading package v1.2.3
Downloading package v1.2.4
Downloading package v1.2.5
```
Becomes:
```
Downloading package v1.2.3
  [repeated 3 times]
```

### Configuration

```yaml
output_compression:
  enabled: false                   # Opt-in (disabled by default)
  min_output_length: 1000          # Minimum chars before compression triggers
  max_compressed_lines: 100        # Target max lines after compression
  excluded_commands: []            # Regex patterns for commands to never compress
  track_savings: true              # Track token savings via /api/metrics/counter
```

### CLI Usage

```
gobby compress [OPTIONS] -- COMMAND...
```

| Option | Description |
|--------|-------------|
| `--stats` | Print compression statistics to stderr |

The CLI runs the command via `subprocess`, compresses the output, and exits with the original command's return code. Statistics format:

```
[compress] strategy=pytest original=12340 compressed=1856 savings=85.0%
```

---

## Code Index Compression

### Overview

A PostToolUse hook that replaces large Read outputs with a compact symbol outline for indexed files. Instead of flooding the context with thousands of lines, the agent gets the first 50 lines plus a symbol map with IDs for targeted retrieval.

### How It Works

1. Agent reads a file via the Read tool
2. Output exceeds the compression threshold (20,000 chars, or `max_file_size_bytes / 5`)
3. The file is indexed in `gobby-code`
4. Output is replaced with:
   - First 50 lines of the file
   - Symbol outline table (name, kind, line range, ID prefix, signature)
   - Instructions for using `get_symbol()` and `search_symbols()`

### Output Format

```
[First 50 lines of original file]

--- Symbol Outline (23 symbols) ---

  function parse_config            [L12-L45]   id: a1b2c3d4 sig: def parse_config(path: str) -> Config
  class    AppServer               [L48-L200]  id: e5f6a7b8 sig: class AppServer:
    method   __init__              [L50-L65]   id: c9d0e1f2 sig: def __init__(self, config: Config)
    method   start                 [L67-L120]  id: 34567890 sig: async def start(self) -> None
  function main                    [L202-L215] id: abcdef01 sig: def main() -> None

To retrieve a specific symbol:
  get_symbol("a1b2c3d4-...")
To search across the index:
  search_symbols("your query")
```

### Enabling

The `compress-large-reads` rule template must be installed and enabled:

- **Rule name:** `code-index-compress-reads`
- **Event:** `after_tool` (Read)
- **Trigger:** Output > 20,000 characters and `code_index_available` is true
- **Effect:** `compress_output` with `compressor: code_index`

Alternatively, the `nudge-on-large-read` rule (trigger: > 10,000 chars) injects a context hint suggesting `gobby-code` tools without replacing the output.

---

## Token Savings

Both compression systems track savings via the `/api/metrics/counter` endpoint:

- **Metric name:** `compression_chars_saved`
- **Labels:** `{"strategy": "strategy_name"}`

Typical savings by category:

| Category | Typical Savings |
|----------|----------------|
| Test output (pytest, jest) | 70–90% |
| Git diff | 60–80% |
| Lint output | 50–70% |
| File reads (code index) | 90–98% |
| Build output | 60–80% |

## When to Use What

| Scenario | System | How |
|----------|--------|-----|
| Verbose shell commands (git, pytest, linters) | Output Compression | `gobby compress -- command` or enable `output_compression.enabled` |
| Reading large indexed source files | Code Index Compression | Enable `compress-large-reads` rule |
| Gentle nudge toward `gobby-code` tools | Nudge Rule | Enable `nudge-on-large-read` rule |
| Custom command output | Output Compression | Falls back to `truncate(head=20, tail=20)` |

## See Also

- [code-index.md](code-index.md) — Code index and `gobby-code` MCP tools
- [rules.md](rules.md) — Rule engine reference
- [testing.md](testing.md) — Token-efficient test infrastructure
- [configuration.md](configuration.md) — Full configuration reference

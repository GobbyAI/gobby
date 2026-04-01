---
name: gsqz
description: >
  Output compressor for LLM token optimization. Wraps shell commands and compresses
  verbose output by 70-95% using pattern-matched pipelines. Use when running tests,
  builds, git operations, linting, or any command that produces more output than an
  LLM needs. Triggers on: compress output, reduce tokens, verbose output, test output,
  build output, token savings, save context, shrink output.
---

# gsqz — Output Compression

Wraps shell commands and compresses output using pattern-matched pipelines. Saves 70-95% of tokens on verbose commands. Always exits 0.

## Usage

Prefix any command with `gsqz --`:

```bash
gsqz -- cargo test
gsqz -- git status
gsqz -- npm run lint
```

When running Bash commands that produce verbose output, use `gsqz --` to compress it. The compressed output preserves errors, failures, and essential information while stripping noise.

## Built-in Pipelines

gsqz auto-detects the command and applies the right compression:

| Category | Commands |
|---|---|
| Git | status, diff, log, push/pull/fetch/clone, add/commit/stash |
| Tests | pytest, cargo test, npm test, vitest, jest, mocha, go test |
| Linters | ruff, mypy, pylint, eslint, tsc, biome, golangci-lint, staticcheck |
| Build | cargo build, go build, next build, webpack, make |
| Package | pip install/list, npm install/ls, uv pip/sync/add |
| Files | ls, tree, find, grep/rg |
| Container | docker ps/images/logs, kubectl logs |
| GitHub | gh pr/issue list/view |
| Fallback | Any unmatched command: keeps first 20 + last 20 lines |

## When NOT to Use

- **Interactive commands** — gsqz captures all output, breaking interactivity
- **Short output** — commands producing <1000 chars pass through uncompressed
- **Piped output** — if downstream needs raw output, don't wrap
- **Exit code checking** — gsqz always exits 0; pass/fail is in the content

## Configuration

```bash
gsqz --dump-config    # See all resolved pipelines and settings
gsqz --stats -- cmd   # Show compression stats on stderr
```

Custom pipelines via config files (layered, first match wins):

- **Global:** `~/.config/gsqz/config.yaml`
- **Project:** `.gsqz.yaml` in project root
- **CLI:** `gsqz --config path/to/file -- cmd`

```yaml
# .gsqz.yaml — add a custom pipeline
pipelines:
  my-tool:
    match: '\bmy-tool\s+run\b'
    steps:
      - filter_lines:
          patterns: ['^DEBUG:', '^\s*$']
      - truncate: { head: 15, tail: 10 }
```

## Install

```bash
cargo install gobby-squeeze --no-default-features
```

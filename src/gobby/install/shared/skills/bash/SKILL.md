---
name: bash
description: "Enforces default Bash coding standards for agents writing or refactoring Bash: interpreter boundaries, quoting, arrays, error propagation, cleanup, security, ShellCheck, shfmt, and Bats. Use before editing Bash unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: bash, shell, sh, bats, shellcheck, shfmt, bashate, strict-mode, bashrc, bash-profile
sources:
  - "Primary: Google Shell Style Guide (CC BY 3.0), https://google.github.io/styleguide/shellguide.html; adapted and independently summarized for Gobby."
  - "Topic-discovery only: TheBushidoCollective/han shell-best-practices at commit cc83b67286cfb1e2c72a2fc72ef88ff735c05e16 (FSL-1.1-ALv2); no source text or code copied."
  - "Topic-discovery only: warpcode/dotfiles bash-style-guide at commit ecd4160277c216cf7e1c24a47ea46c99a1368d2c (no declared repository license); no source text or code copied."
---

# Bash

Default coding standards for Bash. Repo conventions and configured tooling take
precedence. Follow the existing interpreter, supported Bash version, formatter,
lint configuration, test framework, and deployment environment when they are
stricter or more specific.

## Tooling

Run the repo's configured syntax, lint, format, and test commands before
finishing. If the repo has no wrapper commands, use the relevant subset:

- Parse: `bash -n path/to/script.sh`
- Lint: `shellcheck path/to/script.sh`
- Format check: `shfmt -d path/to/script.sh`
- Tests: focused `bats path/to/test.bats` or the repo's shell test runner
- Runtime: execute representative success and failure paths in an isolated
  temporary directory

Do not silence ShellCheck, disable failure handling, or weaken tests to make a
script pass. Explain any targeted directive beside the affected command.

## Configuration And Portability

- Identify the language boundary first: Bash, POSIX `sh`, another shell, or a
  tool-specific command block. Keep Bash syntax out of files executed by `sh`.
- Preserve the repo's shebang. For new scripts, choose an interpreter path that
  matches the deployment contract and declare the minimum Bash version when
  using version-sensitive features.
- Treat `set -euo pipefail` as a policy choice with control-flow consequences.
  Understand each option and write explicit failure handling around expected
  non-zero statuses.
- Resolve paths from `BASH_SOURCE` only when a script must be location-aware;
  avoid silently depending on the caller's working directory.

For shebangs, versions, shell options, sourced files, and environment contracts:
`get_skill_file(name="bash", path="references/configuration-and-portability.md")`

## Quoting And Data

- Quote parameter and command substitutions unless splitting or glob expansion
  is intentional and documented.
- Store command arguments and lists in arrays. Invoke command arrays with
  `"${args[@]}"`; never assemble executable command strings for `eval`.
- Pass positional arguments with `"$@"`. Validate required counts and option
  values before use.
- Read lines with `IFS= read -r`; use NUL delimiters for filenames crossing
  process boundaries.
- Separate declaration from command substitution when the command's exit status
  matters: `local output; output="$(command)"`.

For expansions, arrays, option parsing, filenames, and command construction:
`get_skill_file(name="bash", path="references/quoting-and-data.md")`

## Errors And Cleanup

- Check failures where they occur. Use `if command; then`, `if ! command; then`,
  or an explicit status capture when failure is expected or needs context.
- Enable `pipefail` when pipeline components must all succeed; capture
  `PIPESTATUS` immediately when individual statuses matter.
- Send diagnostics to stderr and return non-zero status from failed functions.
- Register cleanup as soon as a resource is created. Preserve the original exit
  status and make cleanup safe when setup is only partially complete.
- Keep traps composable. Avoid replacing a caller's trap from a sourced library.

For `errexit` edge cases, pipelines, traps, signals, and temporary resources:
`get_skill_file(name="bash", path="references/errors-and-cleanup.md")`

## Security And Processes

- Treat arguments, environment variables, filenames, command output, and config
  files as untrusted until validated for their use context.
- Avoid `eval`, dynamically generated shell, and sourcing writable or untrusted
  files. Use arrays, `case`, and explicit dispatch tables.
- Pass `--` before untrusted positional filenames when the command supports it.
- Create temporary files and directories with `mktemp`; set restrictive
  permissions for sensitive data and remove resources through traps.
- Keep secrets out of arguments, logs, tracing, process listings, and committed
  files.

For injection boundaries, permissions, subprocesses, concurrency, and secrets:
`get_skill_file(name="bash", path="references/security-and-processes.md")`

## Testing

- Test observable exit status, stdout, stderr, filesystem effects, and cleanup.
- Cover empty input, whitespace, glob characters, leading dashes, missing tools,
  failed pipeline stages, signals, and repeated execution where applicable.
- Test every supported Bash version or platform when behavior depends on them.
- Keep static checks and runtime tests: each catches failures the other misses.

For ShellCheck, shfmt, Bats, fixtures, and focused validation selection:
`get_skill_file(name="bash", path="references/testing-and-tooling.md")`

## Design

- Keep shell for orchestration around existing commands and small transformations.
  Move complex data modeling, parsing, concurrency, or long-lived services to a
  structured language used by the repo.
- Keep functions focused, declare locals, and make inputs and outputs explicit.
- Put executable flow in `main` for non-trivial scripts and call `main "$@"`.
- Comment invariants, platform constraints, and non-obvious failure handling.

## Before You Finish

If you touched Bash: verify the intended interpreter parses the file, focused
ShellCheck and format checks pass, runtime tests cover success and failure, and
cleanup/security-sensitive paths behave under the supported environments.

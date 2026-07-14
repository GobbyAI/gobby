# Configuration And Portability

## Establish The Boundary

Inspect the shebang, invocation sites, container or CI image, package scripts,
and deployment hosts before choosing syntax. `bash script.sh` and `sh script.sh`
select different languages regardless of the filename.

For new executable scripts:

- Use the interpreter path required by the target environment.
- Use `#!/usr/bin/env bash` when PATH selection is part of the contract.
- Use a fixed path when the runtime image or operating system guarantees it and
  reproducibility matters more than PATH selection.
- Document a minimum Bash version before using associative arrays, `mapfile`,
  namerefs, case conversion, or version-specific `globasciiranges` behavior.

Run version-dependent tests with every supported version. macOS system Bash and
current Linux Bash often differ substantially.

## Shell Options

Choose options deliberately near the entry point:

```bash
set -u
set -o pipefail
```

Add `set -e` only after reviewing contexts where Bash suppresses or propagates
`errexit`: conditions, negation, command substitutions, functions, subshells,
and pipelines. Expected non-zero statuses belong inside explicit conditionals.

Avoid changing global `IFS`. Set it for a single read operation:

```bash
while IFS= read -r line; do
  process_line "$line"
done < "$input_file"
```

Use `shopt -s` only for behavior the script requires. Restore options in sourced
libraries when changing caller-visible shell state.

## Paths And Environment

Accept paths as arguments or configuration when practical. When a script must
find resources relative to itself, resolve from `BASH_SOURCE[0]` and handle
symlink policy explicitly. Do not assume the caller starts in the repository
root.

Validate required environment variables with a useful message:

```bash
: "${DEPLOY_ENV:?DEPLOY_ENV must be set}"
```

Give optional variables defaults without conflating unset and empty values when
that distinction matters. Export only values child processes require.

## Sourced Libraries

A sourced file executes in the caller's shell. It can change options, traps,
variables, functions, positional arguments, and the working directory.

- Keep libraries free of top-level operational side effects.
- Prefix public functions and globals when collision risk exists.
- Use `return`, not `exit`, for library failures.
- Avoid installing global traps from a library unless the API explicitly owns
  process lifecycle.
- Test a library by sourcing it into a clean shell and into representative callers.

## Generated And Embedded Shell

Identify ownership for shell embedded in YAML, Make, Dockerfiles, package scripts,
or templates. Validate both the outer format and the rendered shell. Preserve
template delimiters and quote at the correct interpretation layer.

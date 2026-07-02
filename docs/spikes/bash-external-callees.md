# Bash External Callee Provenance Spike

## Decision

Bash external callee classification should stay unresolved until command provenance can distinguish shell builtins, functions, aliases, sourced files, and PATH executables.

Syntax alone is not enough. `curl`, `test`, `printf`, or `deploy` may refer to a builtin, function, alias, project script, sourced helper, or external binary depending on runtime shell state.

## Provenance Needed

- Local function definitions and sourced-file graph.
- Shell builtin table for the selected shell.
- Optional PATH snapshot or configured executable roots.
- Alias handling only when aliases are statically declared in the parsed file.
- Project-local executable detection to avoid marking repo scripts external.

## Safe Future Boundary

Classify as external only when a command has no local function/alias/source collision, is not a shell builtin unless explicitly modeled as such, and resolves to a configured PATH executable outside the project root.

## Fail-Closed Cases

- Dynamic command execution through variables, `eval`, command substitution, or arrays.
- Commands provided by sourced files that cannot be statically resolved.
- Aliases from interactive shell configuration.
- PATH-dependent calls when PATH provenance is unavailable.

_Last verified: 2026-05-24_

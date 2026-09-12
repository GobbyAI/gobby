# Navigation and scope

Load when surveying a repository, choosing project/path scope, or traversing
pages. Discover commands through `gcode --help`; inspect existing state with
`gcode status` and `gcode projects` before assuming an index is absent.

Use `gcode repo-outline` for directory statistics, `gcode tree [PATH ...]` for
files and symbol counts, `gcode kinds` for indexed kinds, and `gcode outline`
for one parser-backed file. Content-only indexed files appear with zero symbols.
Choose a file, locate the relevant symbol, then retrieve its body.

Bare paths resolve from the project root. `./` and `../` resolve from cwd;
absolute paths map into the checkout or overlay. Multiple positional paths
and globs use OR semantics, for example
`gcode tree crates/gcode/src 'docs/**/*.md'`. Tree has positional filters, not
grep's `--glob` flag. Use `--project <path-or-name>` to select another indexed
project. Never infer cross-project write authority from successful discovery.

Navigation uses a 2,000-token compact-text page budget by default. Collection
pages contain whole semantic items; an oversized first item remains complete.
Run the exact shell-safe continuation command unchanged while relevant pages
remain. JSON exposes `next_offset` and conditional `budget_exceeded`; explicit
JSON is otherwise unbounded apart from the command's limit defaults. Do not
equate a limited page with the whole repository.

Gcode requires registered Gobby checkout identity and a daemon-issued runtime
grant. Linked worktrees and isolation markers can resolve distinct code-index
IDs; parent metadata is not permission to mutate the parent's index. Standalone
`.gobby/gcode.json` does not register a checkout. Resolve identity errors through
[recovery](recovery.md), never by fabricating IDs or credentials.

For plan targets, resolve exact qualified names before broader impact queries:
`path.py::Class.method` or `path.rs::Type::method`. Use neither UUIDs nor line
numbers as durable plan targets. File-wide targets need a scope reason; follow
the plan coverage reference for its grammar.

Guide: [Operating model](../../../../../../../../docs/guides/gcode-user-guide.md#operating-model).

_Last verified: 2026-09-12_

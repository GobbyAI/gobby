# Impact analysis

Load before changing behavior with callers or dependencies outside the edited
symbol. Discover syntax with `gcode callers --help`, `gcode path --help`, or
`gcode blast-radius --help`.

Resolve a canonical symbol using `search-symbol` and retrieve its body first.
Then use `callers` for incoming calls, `callees` for outgoing calls, `usages`
for incoming call sites, and `imports <file>` for module relationships.
Prefer full stored symbol UUIDs where possible. Graph query resolution fails
closed on ambiguity; choose a candidate or a more precise qualified query.

Use `gcode blast-radius <symbol> --depth 3` to walk transitive impact and
`gcode path <from> <to> --max-depth 8` for a shortest CALLS path. Depth bounds
are real limits on evidence. Graph traversal depends on FalkorDB and indexed
facts; external calls, unresolved endpoints, dynamic dispatch, and callback
references can leave gaps. Use `gcode grep -w "symbol_name"` for callback and
other textual occurrences. A graph is evidence to inspect, not proof that all
affected behavior has been found.

Collection commands support complete-item pagination; follow the printed
continuation. Preserve confidence and truncation diagnostics when reporting
impact. If the symbol is absent, switch to exact lookup or source text. If the
backend is unavailable, use [recovery](recovery.md) and explicitly bound the
impact conclusion to the available evidence.

For file-scoped or structural impact, load [graphs](graphs.md). HTTP clients
have `/api/code-index/graph/blast-radius` (exactly one `symbol_id` or `file_path`)
and `/api/code-index/graph/path`; HTTP defaults differ from CLI defaults, so
consult the route guide rather than translating arguments mechanically.

Guide: [Dependency graph](../../../../../../../../docs/guides/gcode-user-guide.md#dependency-graph).

_Last verified: 2026-09-12_

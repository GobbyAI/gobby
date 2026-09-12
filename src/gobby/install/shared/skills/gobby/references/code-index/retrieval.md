# Source retrieval

Load when reading source after a hit, diagnostic, or known file location.
Discover retrieval syntax with `gcode symbol-at --help` or `gcode symbols --help`.

1. Use `gcode symbol-at path/to/file.py:42` after a file/line hit. The optional
   `:COLUMN` is a 1-based byte column; separate `PATH LINE` is also supported.
2. Use `gcode outline path/to/file.py` for a hierarchical AST map. Load a
   selected body with `gcode symbol <full-uuid>`; use `gcode symbols` for a batch.
3. Get actual full stored UUIDs from `outline --verbose`, `search-symbol
   --verbose`, or JSON results. Never send placeholder IDs, prefixes, or globs.

`symbol-at` chooses a containing visible symbol, otherwise the nearest visible
symbol. Check the fallback diagnostic or JSON `lookup.match_kind`; a nearest
body does not prove it contains the requested line. Exact byte-offset source
retrieval is bounded to the chosen symbol. Fetch tight neighboring context only
when needed after retrieval; do not replace this flow with whole-file reads.

Content-derived IDs change after edits. A missing ID means re-resolve the file
with `outline` or `symbol-at`. Batch retrieval retains valid requested bodies
and reports missing IDs; do not discard valid results or repeatedly retry stale
IDs. Follow complete collection pages using the printed continuation command.

`outline` is AST-only. Markdown and other content-only files can return success
with no symbols and a recovery diagnostic. For headings use
`gcode grep '^#{1,6} ' path/to/file.md -m 200`; use `search-content` for prose.
When retrieval reports contract or payload skew, follow [recovery](recovery.md).

Guide: [Symbol retrieval](../../../../../../../../docs/guides/gcode-user-guide.md#symbol-retrieval).

_Last verified: 2026-09-12_

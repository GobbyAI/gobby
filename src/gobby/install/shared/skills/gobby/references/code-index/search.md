# Search

Load before choosing a search lane or interpreting search failures. Discover
flags with the selected command's `--help`; use `gcode kinds` for current kinds.

| Query shape | Command |
| --- | --- |
| Known symbol | `gcode search-symbol "name"` |
| Exact identifier occurrence | `gcode grep -w "identifier" -m 50` |
| Literal or call site | `gcode grep -F "spawn_ui_server(" -m 50` |
| Ranked repository text, docs, config | `gcode search-content "text"` |
| Fuzzy code concept | `gcode search "concept"` |
| Symbol metadata BM25 | `gcode search-text "query"` |

Locate, narrow by path, then retrieve with `symbol-at` or `outline` and `symbol`.
After empty or irrelevant results, switch according to query shape. Do not
paraphrase the same fuzzy query or page through noise. `search` ranks symbols
only: BM25, available semantic vectors, and graph ranking. `search-content`
ranks indexed text chunks. `search-symbol` resolves exact names first.

Ranked search accepts positional paths/globs with OR semantics, `--language`,
`--limit`, `--offset`, and `--token-budget`. `search` and `search-symbol` also
accept `--kind`; the latter optionally adds graph neighbors with `--with-graph`.
Quote globs so the shell passes them unchanged.

`grep` uses Rust regex: `a|b` means alternation; `a\|b` matches a literal pipe.
Use `-F` for metacharacters such as parentheses and dotted config keys. Use
`-w` for ASCII identifier boundaries, `-l` for files, `-i` for case folding,
`-A/-B/-C` for context, and `-g` for glob filters (ANDed with positional paths).
`-m/--limit` has alias `--max-count`; `-E/-n/-r/-R` are accepted no-ops.
Unsupported filesystem behavior belongs in raw `rg`, not guessed gcode flags.

Text omits UUIDs and ranking diagnostics. Request JSON or `--verbose` only when
needed. Hybrid JSON separates display `score`, raw `rrf_score`, and `sources`;
follow actionable `hint` redirects. A degraded semantic/graph source can leave
valid BM25 results. Empty success is not an error; failures and missing service
configuration require [recovery](recovery.md), not invented matches.

Guide: [Search lanes](../../../../../../../../docs/guides/gcode-user-guide.md#search).

_Last verified: 2026-09-12_

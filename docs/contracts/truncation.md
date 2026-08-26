# Truncation Contract

If you already hold a complete string `S`, and a caller is meant to receive
`S`, you may not return `S[:N]` for `N < len(S)`.

You may return `S`, a pointer to `S`, a whole-item omission list, or (logs
only) an intentional tail of `S`.

This is the residual rule after epic #18364, which banned silent only-copy
destruction. #18364 made unmarked last-hop prefixes legal if they carried a
retrieval path. That is no longer enough: a marked prefix of an existing
payload (`text[:10000] + "\n... [truncated]"`) is still this bug. Generation
caps bound new text rather than forwarded payloads. They do not license
chopping a complete string the caller is meant to receive. In short:
generation caps bound new text.

## Ban

`existing_text[:N]` (or a UTF-8-safe equivalent) is forbidden on a complete
payload the caller is supposed to receive. That includes “helpful” marked
prefixes that keep the first 10K of a 15K string and drop the last 5K.

## Legal conversions

When a payload is too large to inline, use exactly one of:

1. **Return the full text.**
2. **Offload and point** — persist the full bytes; the inline value is an
   envelope, breadcrumb, or path, not a chopped body. Reuse
   `ToolResultOffloader` / `gobby-results`, `get_handoff_context`,
   `get_agent_capture`, or `get_session_messages`.
3. **Omit a whole item** that does not fit (a whole memory, message, or
   contributor), with a deterministic continuation path. Never half-cut one
   item.
4. **Intentional tail** — last N lines of a log, labeled as a tail, and only
   when the product is “show the end of the stream.” If that tail would become
   the only durable copy, persist the full stream first.

Do not add a new MCP server, overflow table, or repo-wide grep linter.

## Allowed kinds

These are not the bug. Do not “fix” them under this contract.

| Kind | Why it is not this bug |
| --- | --- |
| Token / hit budgets (`gcode --token-budget`, collection `limit`/`offset`) | Whole-item pagination with a deterministic retrieval path |
| Generation caps | “Write a summary under 10K” bounds *new* text, not an existing output |
| Intentional log tails | Last N lines of a pane, last 20 lines of a capture excerpt, ACP live tail |
| Pagination (`limit`/`offset`, `get_tool_result` slices) | The rest is one call away |
| Offload envelopes | Inline pointer; full bytes stay in `ToolResultStore` |
| Progressive-discovery briefs (`safe_truncate` / `truncate_tool_brief`) | Catalog card; full description is `get_tool_schema` |
| Typed resource fails (`run_command` `output_limit`) | No partial success |
| Derived storage bounds (`summary_safety` 500-char *generated* summary, `clip_link_field` btree key) | Not forwarding an existing payload |
| UI / CLI ellipsis, progress-bar `truncate_left` | Display chrome |

## Token-budget pagination

A token budget selects the largest complete prefix whose fully rendered page,
including headers and continuation metadata, fits the approximate budget. The
estimate is `ceil(chars / 4)`. Result order stays stable across pages, and the
continuation offset identifies the next semantic item with no gaps or
duplicates.

Page units follow the product domain: one search hit, grep match with context,
outline root subtree, complete symbol, file row, directory group, graph
relationship, or wiki hit. A page never slices one unit. When the first unit
alone exceeds the budget, return it complete and mark the page as over budget.
The response must expose `next_offset`, or text must print an exact continuation
command, whenever more items remain.

## History

- **#18364** — first wave: silent only-copy destruction, unmarked clips,
  typed resource fails.
- **#18813** — oversized MCP results offload to `gobby-results`.
- **#19850** — skill-load bodies must arrive complete.
- **#20333** — memory overflow between the inline budget and the ship limit
  queued to `get_recall_memories` (path retired with automatic recall, #21009).
- **#20395** — this residual sweep: do not prefix-slice existing outputs.

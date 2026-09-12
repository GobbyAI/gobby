# Oversized tool results

Load when a proxy result reports `offloaded`, `retrieval_available`, `result_id`,
or `reason="too_large"`. Discover the `gobby-results` schemas before use.
A preview, structure summary, or search match is not the complete original result.

When retrieval is available, use `search_tool_result(result_id, query)` to locate
relevant chunks. Default search limit is 5, maximum 50. Search is relevance-based,
not an exhaustive page cursor. Use `get_tool_result(result_id, offset=0)` for
ordered raw content; its default slice is 1000 Unicode characters. Keep the ID
and pass each returned `next_offset` until null when complete reading is required.
Do not calculate byte offsets or assume the requested limit was returned: live
envelope limits and JSON escaping can shorten a page.

Results are project-scoped and expire under live retention policy. Missing,
expired, invalid, and foreign IDs can all return not-found. Do not guess IDs or
switch project identity to retrieve another project's result. Use a fresh,
authorized source query when necessary. If a slice cannot advance, report the
retrieval failure rather than repeating the same offset indefinitely.

`call_tool`'s optional `intent` can add matched sections to the initial envelope.
This improves selection; it never proves full reading. Preserve success/failure
state separately from offloading. Failed or non-text results are not ordinary
text-offload candidates; skill/reference delivery and result retrieval have
explicit exemptions so normal instruction pagination remains authoritative.

If `retrieval_available=false`, there is no stored tail to page. Over-storage-cap
responses report `reason="too_large"`; persistence failure may produce a bounded
inline fallback. Narrow a read query or use the source's own pagination. Do not
blindly repeat a mutating source operation to recover its output.

Guidance carries current retention and bounds; configuration and registered
schemas own limits. Tool-result pages are not skill/reference cursor pages and
do not satisfy instruction requirements.

Guide: [Oversized results](../../../../../../../../docs/guides/mcp-tools.md#oversized-results).

_Last verified: 2026-09-12_

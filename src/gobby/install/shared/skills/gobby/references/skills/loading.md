# Load complete instructions

Load before applying an installed skill or implementing loading consumers.
Use `get_skill(name="<installed-name>")`. The initial view defaults to
`brief=true`: instruction content is exact, management metadata is omitted.
Use `brief=false` for identity, provenance, version/hash or management work.

Read `skill.content` and `page.next_cursor`. While the cursor is non-null, make
another `get_skill(cursor="<returned-cursor>")` call with no initial lookup
arguments. Every page needs its own outer result. Emit content together with
page metadata through wrappers; never parallelize skill pages or combine full
skill results. Deduplicate multiple skills preserving required order, then
load them sequentially. Reassemble bytes in order, including whitespace.

Only a completed entrypoint records the skill and its effective level in caller
session state. Listings, menus, partial pages, errors and references do not
count as entrypoint loads. Supply caller `session_id` on the outer tool call;
tracking without usable session context is best effort, not proof of a gate.

On `stale_cursor` or `invalid_cursor`, restart the original lookup without a
cursor and consume every page. Do the same if content is absent or explicitly
truncated. Collapsed UI presentation alone is not truncation. Resolve scan or
lookup errors before applying content; do not bypass failed delivery with a
manual variable write. After a context reset, reload required instructions.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#complete-loading-and-levels).

_Last verified: 2026-09-12_

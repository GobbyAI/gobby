# Discover and load reference files

Load when an entrypoint's topic condition applies, a reference is required,
or an attached-file inventory is needed. Get a schema lease for
`get_skill_file` or `get_skill_files` before calling it; neither is bootstrap.

Use the completed entrypoint's topic index and exact stored path, for example
`get_skill_file(name="gobby", path="references/tasks/closing.md")`. Read
`file.content`; follow each `page.next_cursor` with only `cursor` until null,
one page per outer result. The same restart/truncation rules as entrypoints
apply. A cursor is bound to file identity and content, not interchangeable
with a skill-body cursor.

`get_skill` includes bounded reference/file metadata. For a complete inventory,
call `get_skill_files(name="<name>", path_prefix="references/")`, then repeat
with the same filters and returned `next_after_path` as `after_path`. This is
keyset listing, not a content cursor. Continue until no next path remains.
The list does not load the files it names. Use stored paths; do not normalize
traversal, absolute paths or backslashes into accepted identities.

A completed reference is tracked separately as
`gobby:references/tasks/closing.md`, keyed by skill and exact path. It does not
load the router, another topic or the standalone skill; loading the router or
a menu never satisfies this reference requirement. Shared requirement helpers
must resolve both plain names and exact reference identities. Context resets
clear loaded-reference tracking wherever loaded-skill tracking resets.

If a file is missing, refresh inventory and inspect source/sync provenance.
Restart stale cursors after edits. Do not claim completeness from the initial
bounded manifest or bypass requirements by setting tracking variables.

Guide: [Skills](../../../../../../../../docs/guides/skills.md#reference-files).

_Last verified: 2026-09-12_

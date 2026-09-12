# Read and recover transcripts

Load when retrieving conversation evidence, searching prior activity, or restoring
an archive. Discover `gobby-sessions:get_session_messages`,
`search_session_messages`, `get_session_commits`, `get_transcript_status`, and
`restore_session_transcript`; fetch each needed schema before calling.

Resolve the target identity first. Read chronological message windows using
`limit` and `offset`; inspect returned counts and advance through the required
windows. `full_content` is accepted but unused: message bodies are always full.
It is not a compact-view switch, and `truncated=false` does not mean every
message in the session was returned.

Search requires a nonblank query and positive limit. A session-specific search
scans its rendered windows; multi-session search uses project/status/source
filters and a bounded session scan. A bounded search result is not an exhaustive
repository history. Use explicit session reads for complete evidence. Commit
timeframe results are useful leads; verify actual task linkage and diff evidence
before attributing a change to an agent.

The reader uses live JSONL with gzip archive fallback. Missing messages may mean
an unavailable reader or transcript, not an empty conversation. Inspect
`get_transcript_status` first. Restore to the recorded path or an intentional
`target_path`; the restoration tool reports missing archives, missing paths,
or an already-existing destination instead of proving a restore occurred.

Operator `gobby sessions summarize` produces an archival summary, with optional
file output. It never stages a compact/clear marker and cannot repair an empty
`get_handoff()`. Preserve detailed evidence in transcripts and task records;
summaries are navigation aids.

HTTP clients can inspect session changes/diffs, raw transcripts, transcript
availability, and token events. They can restore archives and generate or update
archival summaries. These client surfaces do not add MCP parameters or stage
handoffs. Use the HTTP guide for route contracts and inspect availability before
assuming a raw file or summary exists.

Guide: [Reading session data](../../../../../../../../docs/guides/sessions.md#reading-session-data).

_Last verified: 2026-09-12_

---
description: Compact handoff summary prompt for cumulative compression
required_variables:
  - transcript_summary
  - last_messages
  - git_status
  - file_changes
optional_variables:
  - previous_summary
  - structured_context
  - git_diff_summary
---
You are creating a session continuation summary after a compaction event.
Compact summaries are for in-session continuity. Be MORE detailed than session-end summaries.
Preserve exact error messages verbatim, not paraphrases.
Keep total response under 1500 words.

## Ground Truth: Structured Data

These are extracted from tool calls and git -- trust these over the transcript.

### Structured Session Data:
{{ structured_context }}

### Git Status:
{{ git_status }}

### Files Changed:
{{ file_changes }}

### Actual Code Changes:
{{ git_diff_summary }}

## Context from Earlier in This Session:
{{ previous_summary }}

If there is previous context above, focus your summary on what happened AFTER
that point. Compress the historical context into a brief "Session History" section.
If no previous context, this is the first segment -- summarize the full session.

## Supplementary: Transcript

The transcript is noisy -- most tool calls are routine reads/searches/globs.
Focus on Edit/Write calls, error messages in tool results, and user requests.

### Current Transcript:
{{ transcript_summary }}

### Last Messages:
{{ last_messages }}

---

Create a continuation summary optimized for resuming work after compaction.
Omit any section entirely if no relevant information exists -- do NOT write "None" for empty sections.

### Current Focus
[Exactly what was being worked on when compaction hit: file, function, specific change.
This is the most important section. Be as specific as possible.]

### Technical State
- Files with uncommitted changes: [list with status]
- Recent commits: [list with SHAs and messages]
- Errors or blockers: [exact error messages if any]

### What Was Just Done
[Bullet points of this segment's actions only, not historical.
Reference specific files, functions, commit SHAs.]

### Discoveries & Dead Ends
[Things learned this segment that aren't obvious from the code.
Include failed approaches and WHY they failed -- this prevents
the next segment from retrying them.]

### Key Decisions
[Decisions made this segment and WHY. Reference specific alternatives
that were considered and rejected.]

### Session History (compressed)
[1-2 sentences if previous_summary exists. Skip if no previous context.]

### Task Progress
- Completed this segment: [task IDs + titles]
- Currently working: [task ID + title + what's done so far on it]
- Remaining: [task IDs in planned order]

### Immediate Next Action
[Single most important thing to do next. Must be specific enough to act on immediately
without additional context -- include file name, function name, and what to do.]

IMPORTANT: Prioritize recency. "Current Focus" and "What Was Just Done"
should be detailed and specific. Historical context should be compressed.
Use only ASCII-safe characters.

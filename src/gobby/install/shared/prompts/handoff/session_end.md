---
description: Session end handoff summary prompt
required_variables:
  - transcript_summary
  - last_messages
  - git_status
  - file_changes
optional_variables:
  - structured_context
  - git_diff_summary
  - claimed_tasks
  - session_memories
  - first_digest_turn
  - recent_digest_turns
---
You are generating a session summary for a future AI agent to resume work. Be precise.
Reference specific file names, function names, error messages, and commit SHAs.
Do NOT use vague phrases like "various improvements", "several fixes", "continued work on".
Keep total response under 2000 words.

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

### Active Tasks (from DB)

{{ claimed_tasks }}

### Memories Stored This Session

{{ session_memories }}

### Session Origin (from Digest)

{{ first_digest_turn }}

### Most Recent Activity (from Digest)

{{ recent_digest_turns }}

## Supplementary: Transcript

The transcript is noisy -- most tool calls are routine reads/searches/globs.
Focus on Edit/Write calls, error messages in tool results, and user requests.
Use this to fill gaps not covered by the structured data above.

### Recent Turns:
{{ transcript_summary }}

### Last Messages:
{{ last_messages }}

---

Create a markdown summary with the following sections. Omit any section entirely if no relevant information exists -- do NOT write "None" for empty sections.

Do NOT include a top-level '# Session Summary' header.

## Current State
[What is working, what is broken, uncommitted changes, failing tests.
This is what the next session needs to know first.]

## Files Changed
[For each file: explain the specific change using diff content. Include file paths.
Do not just list file names -- describe what changed and why.]

## What Was Accomplished
[Bullet points referencing specific files, functions, and commits.
Each bullet should name the file and describe the specific change.]

## Key Technical Decisions
[Decisions and WHY they were made. Reference specific alternatives considered.]

## Problems Encountered
[Errors, failed approaches, exact error messages.]

## What Didn't Work
[Approaches that were tried and abandoned, and WHY they failed.
Different from Problems Encountered (which are blockers).
These are dead ends that future sessions should not retry.]

## Next Steps
[Numbered list. Each item must be actionable without additional context -- include file names, function names, and what specifically to do.]

Use only ASCII-safe characters. Replace: em-dashes with hyphens (-), smart quotes with straight quotes (' "), bullet points with asterisks (*), ellipses with three periods (...).

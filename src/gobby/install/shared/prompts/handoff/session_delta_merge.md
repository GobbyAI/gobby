---
description: Session summary delta merge prompt
required_variables:
  - previous_summary
  - new_digest_turns
  - structured_context
  - git_status
  - file_changes
optional_variables:
  - git_diff_summary
  - claimed_tasks
  - session_memories
  - recent_digest_turns
  - last_messages
---
You are refreshing a handoff summary for a future AI agent. Return a complete replacement summary, not a patch.
Preserve still-valid information from the previous summary. Update current state from structured data and git context.
Incorporate the new digest turns. Remove stale next steps contradicted by the current state.
Reference specific file names, function names, error messages, task refs, and commit SHAs.
Keep total response under 2000 words.

## Previous Summary

{{ previous_summary }}

## New Digest Turns Since Last Summary

{{ new_digest_turns }}

## Current Ground Truth

### Structured Session Data
{{ structured_context }}

### Git Status
{{ git_status }}

### Files Changed
{{ file_changes }}

### Actual Code Changes
{{ git_diff_summary }}

### Active Tasks
{{ claimed_tasks }}

### Memories Stored This Session
{{ session_memories }}

### Recent Digest Context
{{ recent_digest_turns }}

### Last Messages
{{ last_messages }}

---

Create a markdown summary with the same section contract as the session end handoff.
Always include Current State and Next Steps. All other sections are optional; omit an optional section if no relevant information exists. Do NOT write "None" for empty sections.
Do NOT include a top-level '# Session Summary' header.

## Current State
[What is working, what is broken, uncommitted changes, failing tests.
This section is mandatory. This is what the next session needs to know first.
If the work is complete, state completion explicitly.]

## Files Changed
[For each file: explain the specific change using diff content. Include file paths.
Do not just list file names - describe what changed and why.]

## What Was Accomplished
[Bullet points referencing specific files, functions, and commits.
Each bullet should name the file and describe the specific change.]

## Key Technical Decisions
[Decisions and why they were made. Reference specific alternatives considered.]

## Problems Encountered
[Errors, failed approaches, exact error messages.]

## What Didn't Work
[Approaches that were tried and abandoned, and why they failed.
Different from Problems Encountered. These are dead ends that future sessions should not retry.]

## Next Steps
[This section is mandatory. Use a numbered list where each item is actionable without additional context - include file names, function names, and what specifically to do.
If implementation is complete, state the remaining handoff action such as commit, validation, commit linking, or task closure.]

Use only ASCII-safe characters. Replace: em-dashes with hyphens (-), smart quotes with straight quotes (' "), bullet points with asterisks (*), ellipses with three periods (...).

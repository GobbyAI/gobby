---
description: Build strict JSON containing a turn record and session title candidate
required_variables:
  - prompt_text
  - response_text
---
Given a conversation turn, produce a strict JSON object.

## User Prompt
{{ prompt_text }}

## Agent Response
{{ response_text }}

## Instructions
Return only valid JSON with exactly these string fields:
- `turn_markdown`: non-empty markdown record of this turn in chronological order
- `title_candidate`: concise 3-5 word session title candidate

`turn_markdown` must cover:
- What the user asked or requested
- What the agent found, decided, or accomplished
- Each tool used and its purpose (file reads, edits, searches, commands)
- Files created, modified, or deleted
- Commits made (with refs)
- Task operations (created, claimed, closed)
- Key technical findings or decisions

Write in concise past tense. Include specifics (file paths, function names,
task refs like #N, commit SHAs). No filler. Target 200-400 words.

Example:
{"turn_markdown":"User asked...","title_candidate":"Digest JSON Titles"}

---
description: Build a detailed, human-readable record of a single conversation turn
required_variables:
  - prompt_text
  - response_text
---
Given a conversation turn, produce a detailed, human-readable record.

## User Prompt
{{ prompt_text }}

## Agent Response
{{ response_text }}

## Instructions
Produce a structured record of this turn in chronological order:
- What the user asked or requested
- What the agent found, decided, or accomplished
- Each tool used and its purpose (file reads, edits, searches, commands)
- Files created, modified, or deleted
- Commits made (with refs)
- Task operations (created, claimed, closed)
- Key technical findings or decisions

Write in concise past tense. Include specifics (file paths, function names,
task refs like #N, commit SHAs). No filler. Target 200-400 words.

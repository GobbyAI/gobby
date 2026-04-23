---
description: Synthesize memory search results into concise context with ID refs
required_variables:
  - digest
  - memories_json
---
You are synthesizing project memories into concise context for a coding session.

## Current Session Digest
{{ digest }}

## Retrieved Memories
{{ memories_json }}

## Instructions
Synthesize the most relevant memories into 3-5 concise sentences of actionable context. For each memory you reference, include `(ref: mem-XXXXX)` using the first 5 characters of its ID.

Prioritize memories that are directly relevant to the current session digest. Skip memories that are generic or unrelated.

Output ONLY the synthesized context (no headers, no markdown formatting, no XML tags). Example:

When closing gobby tasks that have file edits, commit SHAs are required even for duplicates (ref: mem-3e2f1). The web UI slash command aliases only appear when /mcp/tools returns matching tools (ref: mem-15157). Gobby-managed PreToolUse hooks enforce task-before-write checks (ref: mem-c6534).

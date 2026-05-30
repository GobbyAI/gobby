---
description: Select directly useful candidate memories for current user turn
required_variables:
  - user_prompt
  - memories_json
  - selected_limit
---
You are selecting project memories to surface for the current user turn.

## User Prompt
{{ user_prompt }}

## Candidate Memories
{{ memories_json }}

Choose up to {{ selected_limit }} memory IDs that are directly useful for answering the user's prompt.

Skip generic, weakly related, stale, or merely interesting memories. Prefer zero memories over noisy context.

Output strict JSON only:
{"memory_ids":["mem-id-1","mem-id-2"]}

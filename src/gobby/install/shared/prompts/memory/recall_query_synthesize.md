You are preparing a compact search query for memory recall.

The input is a real user prompt that may include long pasted context, logs, diffs, or
instructions. Extract only the durable facts that would help retrieve relevant stored
memories for this turn.

Rules:
- Preserve specific project names, task IDs, file paths, function names, error names,
  tool names, and explicit user intent.
- Drop boilerplate, protocol text, copied policy, long examples, repeated logs, and
  unrelated pasted content.
- Keep the query under {{ max_query_chars }} characters.

User prompt:
{{ user_prompt }}

Output strict JSON only:
{"query":"short memory search query"}

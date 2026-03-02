---
description: Generate compact handoff context and archival summary from session digest
required_variables:
  - digest_markdown
---
Given a session's complete turn-by-turn digest, produce two outputs.

You MUST separate the two outputs with the exact line `===SECTION_BREAK===` on its own line (no extra whitespace). This marker is required for machine parsing — do not omit it, rename it, or wrap it in markdown formatting.

## Session Digest
{{ digest_markdown }}

---

## Output A: Handoff Context
What the next session needs to know to continue this work.
Include: current state, open problems, key decisions, relevant file paths.
Exclude: anything the CLI's own compaction already preserves.
Keep under 500 words.

===SECTION_BREAK===

## Output B: Session Summary
Archival summary of what was accomplished.
Include: goals, outcomes, commits, tasks closed, key findings.
Keep under 800 words.

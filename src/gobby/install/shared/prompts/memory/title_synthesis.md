---
description: Synthesize a short session title from the turn-by-turn digest
required_variables:
  - digest_markdown
---
Given a session's turn-by-turn digest, produce a 3-5 word title
reflecting the current focus of the session.

## Session Digest
{{ digest_markdown }}

Output only the title, nothing else.

---
description: Generate a short session title from the user's first prompt
required_variables:
  - prompt_text
---
Given a user's first message to an AI coding assistant, generate a 3-5 word title
that captures the intent of their request.

## User Message
{{ prompt_text }}

Output only the title, nothing else. No quotes, no punctuation.

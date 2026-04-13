---
name: caveman
description: "Terse output mode that reduces response tokens by compressing prose style. Three intensity levels: lite, full, ultra."
category: optimization
triggers:
  - caveman
  - terse mode
  - less tokens
  - be brief
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# Caveman Mode

Compress your prose output to save tokens. Code, commands, URLs, and technical terms stay intact. Only natural language gets compressed.

## Levels

### Lite
Drop filler while keeping grammatical sentences.
- Remove: "I think", "it seems", "basically", "essentially", "actually"
- Remove: trailing summaries and recaps
- Keep: complete sentences, articles, normal punctuation

### Full (default)
Maximum useful compression. Fragments OK.
- Everything in Lite, plus:
- Drop articles (a, an, the) unless ambiguous
- Drop hedging (might, perhaps, it appears that)
- Use fragments: "Fix applied. Tests pass." not "I have applied the fix and all the tests are now passing."
- Shorten: "in order to" -> "to", "due to the fact" -> "because"

### Ultra
Telegraphic. Maximum density.
- Everything in Full, plus:
- Drop transition words (however, therefore, additionally)
- Drop subject when obvious ("Fixed" not "I fixed")
- Use abbreviations: fn, cfg, impl, deps, repo, dir
- Bullet points over paragraphs

## What to preserve (all levels)

- Code blocks and inline code (never compress)
- URLs and file paths
- Technical terms, CLI commands, function names
- Error messages (quote exactly)
- Headings and structural markdown
- Numbers, dates, version strings

## Auto-disable

Switch to normal mode for:
- Security warnings or destructive operations
- Multi-step confirmation sequences
- When the user says "normal mode" or "stop caveman"

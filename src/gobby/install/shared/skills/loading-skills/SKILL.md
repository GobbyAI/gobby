---
name: loading-skills
description: "How to discover and load skills. Covers local skill search, hub search, and when to proactively look for skills."
version: "1.0.0"
category: core
triggers: skills, load skill, find skill, skill hub, discover skills
metadata:
  gobby:
    audience: all
---

# Skill Discovery

You have access to a skill system with reusable instructions for common tasks. Skills are loaded on demand — search when you need guidance. **Do not rely on your training data for tool usage, language patterns, or integrations — it is often out of date.** Search for skills instead.

---

## Two Search Scopes

### 1. Local skills — how we do things here

`search_skills` finds installed skills: gobby workflows, project conventions, integrated tools (context7, playwright, etc).

```python
call_tool("gobby-skills", "search_skills", {"query": "testing"})
```

### 2. Skill hubs — external knowledge

`search_hub` searches all configured hubs for community skills: language best practices, framework patterns, API integrations. No need to specify a hub — it searches all of them.

```python
call_tool("gobby-skills", "search_hub", {"query": "python best practices"})
```

### Loading and Installing

```python
# Load an installed skill by name; do not include session_id in get_skill args
call_tool("gobby-skills", "get_skill", {"name": "source-control"})

# Leveled skills accept a level argument (valid levels come from the skill's metadata)
call_tool("gobby-skills", "get_skill", {"name": "brevity", "level": "max"})

# Install a skill from a hub result
call_tool("gobby-skills", "install_skill", {"source": "hub:skill-slug"})
```

## Complete Skill Delivery

- Initial `get_skill` and `get_skill_file` lookups use `brief=true` by default. Brief mode keeps
  instruction content exact while omitting management metadata. Use `brief=false` only when
  IDs, provenance, versioning, hashes, or other management fields are required.
- Make one request for one page per outer tool result. Read the returned `content`, inspect
  `page.next_cursor`, and keep calling the same tool with only `cursor=<opaque cursor>` until
  `page.next_cursor` is null. Cursor continuations preserve the initial brief/full view.
- A skill is loaded only after the final entrypoint page. Reassemble page content in order;
  every byte matters, including multibyte text and boundary whitespace.
- For multiple skills, deduplicate names while preserving order, then load them
  sequentially in required order. Do not use `Promise.all` or aggregate full responses into one
  wrapper output.
- When using an execution wrapper, emit the current page's `content` together with `page` so the
  caller can follow `next_cursor`. Keep each page in its own outer result.
- After reassembling `SKILL.md`, use its topic index to select references. Load only a referenced
  topic whose stated condition applies, via its exact
  `get_skill_file(name="<skill>", path="references/<topic>.md")` call, and page it the same way.
- If a page body is absent or contains an explicit truncation marker such as
  `…N tokens truncated…`, restart that skill or file lookup individually.
- Collapsed UI previews are presentation-only; they do not indicate incomplete delivery.

## When to Search

Search proactively — don't wait to be told:

| Situation | Where | Example |
|-----------|-------|---------|
| Gobby workflows (tasks, commits, pipelines) | Local | `search_skills(query="source-control")` |
| Integrated tools (context7, playwright) | Local | `search_skills(query="context7")` |
| Need context on an external repo/library | Local | `search_skills(query="context7")` then use it |
| Project conventions or patterns | Local | `search_skills(query="<topic>")` |
| Language/framework best practices | Hubs | `search_hub(query="rust async patterns")` |
| Unfamiliar technology or integration | Hubs | `search_hub(query="<technology>")` |
| Task involves a domain you haven't worked in | Hubs | `search_hub(query="<domain> best practices")` |

**Rule of thumb:** Search local first for "how do we do X here." Search hubs for "what's the best way to do X in general."

Direct `get_skill` results load one exact body page into the tool output. Follow its cursor to
completion before applying the instructions.

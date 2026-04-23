---
name: loading-skills
description: "How to discover and load skills. Covers local skill search, hub search, and when to proactively look for skills."
category: core
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

# Install a skill from a hub result
call_tool("gobby-skills", "install_skill", {"source": "hub:skill-slug"})
```

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

Direct get_skill results load the skill body into your tool output; follow them before continuing.

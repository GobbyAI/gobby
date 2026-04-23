---
name: context7
description: Instructions for using context7 MCP to look up library documentation before writing code. Loaded on demand on first code file write/edit.
category: core
metadata:
  gobby:
    audience: all
---

# Context7 — Library Documentation Lookup

Use the **context7** MCP server to fetch up-to-date documentation for any library or framework before writing code that uses it. This prevents hallucinated APIs and outdated patterns.

## Workflow

Two-step process — resolve the library, then query its docs:

### 1. Resolve the library ID

```
call_tool("context7", "resolve-library-id", {
  "libraryName": "Next.js",
  "query": "server components and data fetching"
})
```

Returns a list of matching libraries with Context7-compatible IDs (format: `/org/project`).

### 2. Query documentation

```
call_tool("context7", "query-docs", {
  "libraryId": "/vercel/next.js",
  "query": "how to use server components for data fetching"
})
```

Returns relevant documentation snippets and code examples.

## When to use

| Situation | Action |
|---|---|
| Writing code that uses a library API | Resolve + query before writing |
| Unsure about current API patterns | Query with a specific question |
| Choosing between library approaches | Query to compare documented patterns |
| Upgrading or migrating library usage | Query the target version's docs |

## Tips

- **Be specific in queries.** "How to set up JWT authentication in Express.js" beats "auth".
- **Use the official library name** with proper punctuation — "Next.js" not "nextjs", "Three.js" not "threejs".
- **Max 3 calls per question.** If you can't find what you need after 3 calls, use your best result.
- **Version-specific docs** — if the user specifies a version, use the `/org/project/version` format from resolve results.

# TDD Mode Instructions

**IMPORTANT:** Do NOT create separate test/implement/refactor tasks. The system handles TDD structure automatically via the sandwich pattern.

## How It Works

The system automatically applies TDD sandwich pattern after expansion:
- ONE [TEST] task at the start (covers all code/config implementations)
- Your subtasks become the implementation tasks
- ONE [REFACTOR] task at the end

**Your job:** Output plain feature tasks with `category: "code"` or `category: "config"`.

## Categories for TDD

| Category | TDD Treatment | Description |
|----------|---------------|-------------|
| `code` | Yes - wrapped in sandwich | Source code implementation |
| `config` | Yes - wrapped in sandwich | Configuration file changes |
| `docs` | No - stays as single task | Documentation tasks |
| `test` | No - stays as single task | Test infrastructure tasks |
| `research` | No - stays as single task | Investigation/exploration |
| `planning` | No - stays as single task | Design/architecture work |
| `refactor` | No - stays as single task | Behavior-preserving code restructuring |

## DO NOT Use These Prefixes

- "Write tests for:", "Test:", "[TDD]"
- "Implement:", "[IMPL]"
- "Refactor:", "[REF]"

## Example Output

```json
{
  "subtasks": [
    {"title": "Create database schema", "category": "code"},
    {"title": "Add user authentication", "category": "code", "depends_on": [0]},
    {"title": "Document the API", "category": "docs"}
  ]
}
```

The system transforms this into:
- [TEST] Write tests for: Parent Task (covers database schema + authentication)
- [IMPL] Create database schema
- [IMPL] Add user authentication
- [REFACTOR] Refactor: Parent Task
- Document the API (no TDD - it's docs)

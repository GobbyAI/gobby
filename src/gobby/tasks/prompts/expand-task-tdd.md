# TDD Mode Instructions

Do not create separate test/implement/refactor tasks. New expansion specs emit
one implementation leaf per manifest entry. TDD is enforced through task
metadata, developer skills, and review evidence.

## How It Works

For each TDD-required `code` or eligible `config` deliverable:

- emit one implementation task;
- add `additional_skills: ["test-driven-development"]`;
- add label `tdd:required` when the output shape supports labels;
- require validation evidence for red failure, minimal green pass,
  refactor/final-green pass, exact test command, and test-quality audit output
  for supported touched tests. Outside Gobby, unsupported-language warnings
  must be paired with focused repo-native validation.

Code tasks must include `implementation_domain`:

- `backend` routes to `backend-developer`;
- `frontend` routes to `frontend-developer`;
- `fullstack` routes to `fullstack-developer`.

## Categories for TDD

| Category | TDD Treatment | Description |
|----------|---------------|-------------|
| `code` | Required when behavior changes | Source code implementation |
| `config` | Conditional | Configuration work only when executable behavior can be pinned first |
| `docs` | No | Documentation tasks |
| `test` | No | Standalone test infrastructure, characterization, parity, or regression suites |
| `refactor` | No | Behavior-preserving code restructuring |

## DO NOT Use These Prefixes

- "Write tests for:", "Test:", "[TDD]"
- "Implement:", "[IMPL]"
- "Refactor:", "[REF]"

## Example Output

```json
{
  "subtasks": [
    {
      "title": "Create database schema",
      "category": "code",
      "implementation_domain": "backend",
      "additional_skills": ["test-driven-development"],
      "labels": ["tdd:required"]
    },
    {
      "title": "Document the API",
      "category": "docs"
    }
  ]
}
```

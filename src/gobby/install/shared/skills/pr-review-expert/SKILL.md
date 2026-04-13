---
name: pr-review-expert
description: Structured PR review methodology with blast radius analysis, security scanning, test coverage delta, and breaking change detection. Use when reviewing pull requests, analyzing code changes, or assessing code quality of diffs.
category: engineering
tags:
  - gobby
metadata:
  gobby:
    audience: all
  attribution:
    source: alirezarezvani/claude-skills
    original_name: pr-review-expert
    license: MIT
---

# PR Review Expert

Structured, systematic code review for GitHub PRs. Goes beyond style nits — this skill
performs blast radius analysis, security scanning, breaking change detection, and test coverage delta
calculation. Produces a reviewer-ready report with a 30+ item checklist and prioritized findings.

## When to Use

- Before merging any PR that touches shared libraries, APIs, or DB schema
- When a PR is large (>200 lines changed) and needs structured review
- Security-sensitive code paths (auth, payments, PII handling)
- After an incident — review similar PRs proactively

## Fetching the Diff

```bash
# View diff in terminal
gh pr diff <PR_NUMBER>

# Get PR metadata
gh pr view <PR_NUMBER> --json title,body,labels,assignees,milestone

# List files changed
gh pr diff <PR_NUMBER> --name-only

# Check CI status
gh pr checks <PR_NUMBER>

# Download diff to file for analysis
gh pr diff <PR_NUMBER> > /tmp/pr-<PR_NUMBER>.diff
```

## Workflow

### Step 1 — Fetch Context

```bash
PR=123
gh pr view $PR --json title,body,labels,milestone,assignees | jq .
gh pr diff $PR --name-only
gh pr diff $PR > /tmp/pr-$PR.diff
```

### Step 2 — Blast Radius Analysis

For each changed file, identify:

1. **Direct dependents** — who imports this file?
```bash
# Find all files importing a changed module
grep -r "from changed_module import\|import changed_module" . --include="*.py" -l
```

2. **Service boundaries** — does this change cross a service?
```bash
# Check if changed files span multiple services (monorepo)
gh pr diff $PR --name-only | cut -d/ -f1-2 | sort -u
```

3. **Shared contracts** — types, interfaces, schemas
```bash
gh pr diff $PR --name-only | grep -E "types/|interfaces/|schemas/|models/"
```

**Blast radius severity:**
- CRITICAL — shared library, DB model, auth middleware, API contract
- HIGH     — service used by >3 others, shared config, env vars
- MEDIUM   — single service internal change, utility function
- LOW      — UI component, test file, docs

### Step 3 — Security Scan

```bash
DIFF=/tmp/pr-$PR.diff

# SQL Injection — raw query string interpolation
grep -n "query\|execute\|raw(" $DIFF | grep -E '\$\{|f"|%s|format\('

# Hardcoded secrets
grep -nE "(password|secret|api_key|token|private_key)\s*=\s*['\"][^'\"]{8,}" $DIFF

# AWS key pattern
grep -nE "AKIA[0-9A-Z]{16}" $DIFF

# XSS vectors
grep -n "dangerouslySetInnerHTML\|innerHTML\s*=" $DIFF

# Auth bypass patterns
grep -n "bypass\|skip.*auth\|noauth\|TODO.*auth" $DIFF

# Insecure hash algorithms
grep -nE "md5\(|sha1\(|createHash\(['\"]md5|createHash\(['\"]sha1" $DIFF

# eval / exec
grep -nE "\beval\(|\bexec\(|\bsubprocess\.call\(" $DIFF

# Path traversal risk
grep -nE "path\.join\(.*req\.|readFile\(.*req\." $DIFF
```

### Step 4 — Test Coverage Delta

```bash
# Count source vs test files changed
CHANGED_SRC=$(gh pr diff $PR --name-only | grep -vE "\.test\.|\.spec\.|__tests__|test_")
CHANGED_TESTS=$(gh pr diff $PR --name-only | grep -E "\.test\.|\.spec\.|__tests__|test_")

echo "Source files changed: $(echo "$CHANGED_SRC" | wc -w)"
echo "Test files changed:   $(echo "$CHANGED_TESTS" | wc -w)"

# Lines of new logic
LOGIC_LINES=$(grep "^+" /tmp/pr-$PR.diff | grep -v "^+++" | wc -l)
echo "New lines added: $LOGIC_LINES"
```

**Coverage delta rules:**
- New function without tests -> flag
- Deleted tests without deleted code -> flag
- Coverage drop >5% -> block merge
- Auth/payments paths -> require 100% coverage

### Step 5 — Breaking Change Detection

#### API Contract Changes
```bash
# REST route removals or renames
grep "^-" /tmp/pr-$PR.diff | grep -E "router\.(get|post|put|delete|patch)\("

# TypeScript interface removals
grep "^-" /tmp/pr-$PR.diff | grep -E "^-\s*(export\s+)?(interface|type) "
```

#### DB Schema Changes
```bash
# Migration files added
gh pr diff $PR --name-only | grep -E "migrations?/|alembic/"

# Destructive operations
grep -E "DROP TABLE|DROP COLUMN|ALTER.*NOT NULL|TRUNCATE" /tmp/pr-$PR.diff

# Index removals (perf regression risk)
grep "DROP INDEX\|remove_index" /tmp/pr-$PR.diff
```

#### Config / Env Var Changes
```bash
# New env vars referenced in code (might be missing in prod)
grep "^+" /tmp/pr-$PR.diff | grep -oE "os\.environ\[|os\.getenv\(" | sort -u

# Removed env vars (could break running instances)
grep "^-" /tmp/pr-$PR.diff | grep -oE "os\.environ\[|os\.getenv\(" | sort -u
```

### Step 6 — Performance Impact

```bash
# N+1 query patterns (DB calls inside loops)
grep -n "\.find\|\.findOne\|\.query\|db\." /tmp/pr-$PR.diff | grep "^+" | head -20

# Unbounded loops
grep -n "while (True\|while True" /tmp/pr-$PR.diff | grep "^+"

# Missing await — regex is unreliable for this; use a linter or AST tool
# (e.g., pylint, ESLint no-floating-promises, or ruff async checks) instead
```

## Complete Review Checklist

### Scope & Context
- [ ] PR title accurately describes the change
- [ ] PR description explains WHY, not just WHAT
- [ ] Linked ticket exists and matches scope
- [ ] No unrelated changes (scope creep)
- [ ] Breaking changes documented in PR body

### Blast Radius
- [ ] Identified all files importing changed modules
- [ ] Cross-service dependencies checked
- [ ] Shared types/interfaces/schemas reviewed for breakage
- [ ] New env vars documented
- [ ] DB migrations are reversible

### Security
- [ ] No hardcoded secrets or API keys
- [ ] SQL queries use parameterized inputs
- [ ] User inputs validated/sanitized before use
- [ ] Auth/authorization checks on all new endpoints
- [ ] No XSS vectors
- [ ] New dependencies checked for known CVEs
- [ ] No sensitive data in logs (PII, tokens, passwords)

### Testing
- [ ] New public functions have unit tests
- [ ] Edge cases covered (empty, null, max values)
- [ ] Error paths tested (not just happy path)
- [ ] Integration tests for API endpoint changes
- [ ] No tests deleted without clear reason

### Breaking Changes
- [ ] No API endpoints removed without deprecation
- [ ] No required fields added to existing API responses
- [ ] No DB columns removed without two-phase migration plan
- [ ] Backward-compatible for external API consumers

### Performance
- [ ] No N+1 query patterns introduced
- [ ] DB indexes added for new query patterns
- [ ] No unbounded loops on potentially large datasets
- [ ] Async operations correctly awaited

### Code Quality
- [ ] No dead code or unused imports
- [ ] Error handling present (no bare empty catch blocks)
- [ ] Consistent with existing patterns and conventions

## Output Format

Structure your review as:

```
## PR Review: [PR Title] (#NUMBER)

Blast Radius: HIGH — changes lib/auth used by 5 services
Security: 1 finding (medium severity)
Tests: Coverage delta +2%
Breaking Changes: None detected

--- MUST FIX (Blocking) ---

1. SQL Injection risk in src/db/users.py:42
   Raw string interpolation in WHERE clause.
   Fix: use parameterized query

--- SHOULD FIX (Non-blocking) ---

2. Missing auth check on POST /api/admin/reset
   No role verification before destructive operation.

--- SUGGESTIONS ---

3. N+1 pattern in src/services/reports.py:88
   find_user() called inside loop — batch with find_many_users(ids)

--- LOOKS GOOD ---
- Test coverage for new auth flow is thorough
- DB migration has proper rollback
- Error handling consistent with rest of codebase
```

## Common Pitfalls

- **Reviewing style over substance** — let the linter handle style; focus on logic, security, correctness
- **Missing blast radius** — a 5-line change in a shared utility can break 20 services
- **Approving untested happy paths** — always verify error paths have coverage
- **Ignoring migration risk** — NOT NULL additions need a default or two-phase migration
- **Skipping large PRs** — if a PR is too large to review properly, request it be split

## Best Practices

1. Read the linked ticket before looking at code — context prevents false positives
2. Check CI status before reviewing — don't review code that fails to build
3. Prioritize blast radius and security over style
4. Label each comment clearly: "nit:", "must:", "question:", "suggestion:"
5. Batch all comments in one review round — don't trickle feedback
6. Acknowledge good patterns, not just problems

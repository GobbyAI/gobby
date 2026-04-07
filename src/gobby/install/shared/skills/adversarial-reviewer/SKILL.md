---
name: adversarial-reviewer
description: Adversarial code review through hostile reviewer personas that catch blind spots. Use when you want a genuinely critical review of recent changes, before merging a PR, or when you suspect the review is being too agreeable about code quality.
category: engineering
tags:
  - gobby
metadata:
  gobby:
    audience: all
  attribution:
    source: alirezarezvani/claude-skills
    original_name: adversarial-reviewer
    license: MIT
---

# Adversarial Code Reviewer

Forces genuine perspective shifts through three hostile reviewer personas (Saboteur, New Hire, Security Auditor). Findings are severity-classified and cross-promoted when caught by multiple personas.

## Problem This Solves

When an LLM reviews code it wrote (or code it just read), it shares the same mental model, assumptions, and blind spots as the author. This produces "looks good to me" reviews on code that a fresh human reviewer would flag immediately.

This skill forces a perspective shift by requiring adversarial personas — each with different priorities, different fears, and different definitions of "bad code."

## Review Workflow

### Step 1: Gather the Changes

- **Default:** Run `git diff` (unstaged) + `git diff --cached` (staged). If both empty, run `git diff HEAD~1`.
- **Branch review:** Run `git diff main...HEAD`.
- **Specific file:** Read the entire file.

If no changes are found, stop and report: "Nothing to review."

### Step 2: Read the Full Context

For every file in the diff:
1. Read the **full file** — bugs hide in how new code interacts with existing code.
2. Identify the **purpose** of the change: bug fix, new feature, refactor, config change, test.
3. Note any **project conventions** from CLAUDE.md or existing patterns.

### Step 3: Run All Three Personas

Execute each persona sequentially. Each persona SHOULD find at least one issue. If the code is genuinely clean, say so with a brief explanation of what you verified rather than manufacturing false findings.

**Do not soften findings. Do not hedge. Be direct.**

### Step 4: Deduplicate and Synthesize

After all three personas have reported:
1. Merge duplicate findings (same issue caught by multiple personas).
2. Promote findings caught by 2+ personas to the next severity level.
3. Produce the final structured output.

## The Three Personas

### Persona 1: The Saboteur

**Mindset:** "I am trying to break this code in production."

**Priorities:**
- Input that was never validated
- State that can become inconsistent
- Concurrent access without synchronization
- Error paths that swallow exceptions or return misleading results
- Assumptions about data format, size, or availability that could be violated
- Off-by-one errors, integer overflow, null/undefined dereferences
- Resource leaks (file handles, connections, subscriptions, listeners)

**Review Process:**
1. For each function/method changed, ask: "What is the worst input I could send this?"
2. For each external call, ask: "What if this fails, times out, or returns garbage?"
3. For each state mutation, ask: "What if this runs twice? Concurrently? Never?"
4. For each conditional, ask: "What if neither branch is correct?"

---

### Persona 2: The New Hire

**Mindset:** "I just joined this team. I need to understand and modify this code in 6 months with zero context from the original author."

**Priorities:**
- Names that don't communicate intent
- Logic that requires reading 3+ other files to understand
- Magic numbers, magic strings, unexplained constants
- Functions doing more than one thing
- Missing type information that forces tracing through call chains
- Inconsistency with surrounding code style or project conventions
- Tests that test implementation details instead of behavior
- Comments that describe *what* (redundant) instead of *why* (useful)

**Review Process:**
1. Read each changed function as if you've never seen the codebase. Can you understand what it does from the name, parameters, and body alone?
2. Trace one code path end-to-end. How many files do you need to open?
3. Check: would a new contributor know where to add a similar feature?
4. Look for implicit knowledge baked into the code that the reader won't have.

---

### Persona 3: The Security Auditor

**Mindset:** "This code will be attacked. My job is to find the vulnerability before an attacker does."

**OWASP-Informed Checklist:**

| Category | What to Look For |
|----------|-----------------|
| **Injection** | SQL, NoSQL, OS command, LDAP — any place user input reaches a query or command without parameterization |
| **Broken Auth** | Hardcoded credentials, missing auth checks on new endpoints, session tokens in URLs or logs |
| **Data Exposure** | Sensitive data in error messages, logs, or API responses; missing encryption at rest or in transit |
| **Insecure Defaults** | Debug mode left on, permissive CORS, wildcard permissions, default passwords |
| **Missing Access Control** | IDOR (can user A access user B's data?), missing role checks, privilege escalation paths |
| **Dependency Risk** | New dependencies with known CVEs, pinned to vulnerable versions |
| **Secrets** | API keys, tokens, passwords in code, config, or comments |

**Review Process:**
1. Identify every trust boundary the code crosses (user input, API calls, database, file system, environment variables).
2. For each boundary: is input validated? Is output sanitized? Is the principle of least privilege followed?
3. Check: could an authenticated user escalate privileges through this change?
4. Check: does this change expose any new attack surface?

## Severity Classification

| Severity | Definition | Action Required |
|----------|-----------|-----------------|
| **CRITICAL** | Will cause data loss, security breach, or production outage. Must fix before merge. | Block merge. |
| **WARNING** | Likely to cause bugs in edge cases, degrade performance, or confuse future maintainers. Should fix before merge. | Fix or explicitly accept risk with justification. |
| **NOTE** | Style issue, minor improvement opportunity, or documentation gap. Nice to fix. | Author's discretion. |

**Promotion rule:** A finding flagged by 2+ personas is promoted one level (NOTE -> WARNING, WARNING -> CRITICAL).

## Output Format

```markdown
## Adversarial Review: [brief description of what was reviewed]

**Scope:** [files reviewed, lines changed, type of change]
**Verdict:** BLOCK / CONCERNS / CLEAN

### Critical Findings
[If any — these block the merge]

### Warnings
[Should-fix items]

### Notes
[Nice-to-fix items]

### Summary
[2-3 sentences: what's the overall risk profile? What's the single most important thing to fix?]
```

**Verdict definitions:**
- **BLOCK** — 1+ CRITICAL findings. Do not merge until resolved.
- **CONCERNS** — No criticals but 2+ warnings. Merge at your own risk.
- **CLEAN** — Only notes. Safe to merge.

## Anti-Patterns

| Anti-Pattern | Why It's Wrong |
|-------------|---------------|
| "LGTM, no issues found" | If you found nothing, you probably didn't look hard enough. Every change has at least one risk, assumption, or improvement opportunity. |
| Cosmetic-only findings | Reporting only whitespace/formatting while missing a null dereference is worse than no review at all. |
| Pulling punches | "This might possibly be a minor concern..." — No. Be direct. "This will throw when `user` is None." |
| Restating the diff | "This function was added to handle authentication" is not a finding. What's WRONG with how it handles authentication? |
| Ignoring test gaps | New code without tests is a finding. Always. |
| Reviewing only the changed lines | Bugs live in the interaction between new code and existing code. Read the full file. |

## The Self-Review Trap

You are likely reviewing code you just wrote or just read. Your weights formed the same mental model that produced this code. You will naturally think it looks correct because it matches your expectations.

**To break this pattern:**
1. Read the code **bottom-up** (start from the last function, work backward).
2. For each function, state its contract **before** reading the body. Does the body match?
3. Assume every variable could be null/undefined until proven otherwise.
4. Assume every external call will fail.
5. Ask: "If I deleted this change entirely, what would break?" — if the answer is "nothing," the change might be unnecessary.

## When to Use This

- **Before merging any PR** — especially self-authored PRs with no human reviewer
- **After a long coding session** — fatigue produces blind spots; this skill compensates
- **When the initial review was too easy** — if you got a clean pass, run this for a second opinion
- **On security-sensitive code** — auth, payments, data access, API endpoints

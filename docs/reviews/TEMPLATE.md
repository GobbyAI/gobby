# Review Template

> Copy this shape into `docs/reviews/<area>.md`. Expand sections as the area
> warrants; don't drop the header or the systemic-patterns section.

---

# Review: <area>

- **Scope:** `<paths reviewed>` (and the split boundary, if this area was split)
- **Reviewer:** <model / agent>
- **Commit / branch:** <sha or branch reviewed>
- **Summary:** <N Blocker · M Important · K Nit> — <one-line health read>

> If clean: `**No findings.** Reviewed <paths>; confidence <high/med/low>. <why>.`

## Findings

### [BLOCKER] <short title>

- **Where:** `path/to/file.py:123`
- **Failure mode:** <what actually goes wrong, and under what conditions>
- **Why it matters:** <impact — data loss, crash, contract violated, etc.>
- **Minimal fix:** <smallest correct change; name the function/approach>
- **Confidence:** high | med | low — <what would confirm it, if low>

### [IMPORTANT] <short title>

- **Where:** `path/to/file.py:45-60`
- **Failure mode:** ...
- **Minimal fix:** ...
- **Confidence:** ...

### [NIT] <short title>

- **Where:** `path/to/file.py:88`
- **Note:** ...

## Systemic patterns

<Issues that recur across files in this area — a repeated anti-pattern, a missing
abstraction, a contract everyone violates the same way. These are the highest-value
output: one systemic entry can subsume a dozen point findings. If none, say "none.">

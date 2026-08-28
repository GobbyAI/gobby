# Live mode

Call `get_skill_file(name="impeccable", path="references/live-contract.md")` before entering the poll loop.

- On `generate`, call `get_skill_file(name="impeccable", path="references/live-generation.md")`; it routes to `references/live-variants.md` only after planning.
- On fallback, accept, discard, steer, prefetch, manual edit, or abort, call `get_skill_file(name="impeccable", path="references/live-actions.md")`.
- For start failures, configuration, drift, exit, cleanup, or first-time setup, call `get_skill_file(name="impeccable", path="references/live-setup-recovery.md")`.

Load the contract plus only the action-specific reference and recovery reference when needed. Preserve the poll contract, completion signals, verification after accept, and cleanup behavior.

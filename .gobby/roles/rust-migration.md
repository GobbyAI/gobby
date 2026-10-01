# Rust Migration

You own the Rust Migration session (Josh's "Rust Migration" tab) and the implementation work the Orchestrator routes to you.
Claim, implement, validate, commit, then submit to the Orchestrator, who routes the candidate to a Code Reviewer; the reviewer's LAND goes to the Merge Manager, which lands it. Restart the daemon or promote binaries only when Josh or the Orchestrator directs, always with global notices before and after, and never during quiet hours.
- For every crate binary release, bump that crate's `Cargo.toml` patch version by +0.0.1 and include the `Cargo.lock` update in the release commit. For gcode, keep `MIN_GCODE_PRUNE_BUDGET_VERSION` in `src/gobby/code_index/gcode_gateway.py` equal to the crate version; for gdaemon, update `MANAGED_BIN_VERSION_PINS` to match.

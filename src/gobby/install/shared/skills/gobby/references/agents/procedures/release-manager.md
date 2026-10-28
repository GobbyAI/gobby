# release-manager procedures

- Rebuild only the Rust binaries whose crates changed. Load the `rust` skill before editing Rust; `crates/AGENTS.md` holds the conventions.
- A shipped binary whose crate or recursive path dependencies changed gets a 0.0.1 patch-version bump in its shipping commit, with Cargo.lock updated.
- Promote the rebuilt set with `promote_workspace_binary_set` (`src/gobby/install/bin_set_coherence.py`). Never copy binaries by hand.
- Read each promoted binary's sha256 from `~/.gobby/bin/`, never from `target/release/`.
- Report each promotion to the Orchestrator with the binary names, versions and sha256.
- You never run daemon commands. The Orchestrator owns the daemon.
- Never remove dirty worktrees or branches that hold unlanded work. Never push or merge into the release branch.

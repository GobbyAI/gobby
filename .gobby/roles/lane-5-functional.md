# Lane 5 functional developer: gobby#14642

Take Orchestrator-assigned, bounded gclient and terminal fixes. Claim each task and work
in an isolated worktree. Coordinate exact shared paths with Fable (gobby#14607)
and Lane 1 (gobby#14544) before editing. Follow `_common.md`; do not manually
spawn agents or restart the daemon. Run heavy commands only in a slot granted by
the Lane Manager (gobby#14556). Validate and commit each change, then send the
candidate to the Orchestrator and assigned Reviewer with the exact commit and tests.

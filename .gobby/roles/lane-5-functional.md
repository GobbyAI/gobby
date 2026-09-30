# Lane 5 functional developer

Take Orchestrator-assigned, bounded functional-stability fixes. Current work is
#23059 (Codex task-close reviewer startup and denial delivery), followed by
#23055 (Ask retirement) after its migration dependency lands. Claim each task and
work in an isolated worktree. Coordinate exact shared paths with their active
owners before editing. Follow `_common.md`; do not manually spawn agents or
restart the daemon. Run heavy commands only in a slot granted by the Lane Manager
(seat named in `roster.md`). Validate and commit each change, then send the candidate to the
Orchestrator and assigned Reviewer with the exact commit and tests.

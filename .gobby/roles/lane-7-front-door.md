# Lane 7 developer (Rust front door)

Your session is the one named for this file in `roster.md`. Own the front-door lane, epic #21543 (Rust front door). Follow `_common.md`.

- First task: #22951, plan repair, authoring and current-code readiness. Receive the existing clean plan worktree from the Plan Writer (gobby#14578); don't start a new one.
- You may author that plan and run its one previously authorized `plan-enhancer-taskless` pass, only in a slot the Lane Manager (gobby#14556) grants. Spawn nothing else.
- Approval order (Josh's flow, memory 55b8c14e): Orchestrator dispositions of the enhancer edits, then Plan Adversary consensus and M1, then Orchestrator review, then Josh's approval, then Orchestrator expansion. Write no front-door implementation until that sequence approves the plan. After approval you own the implementation.
- For assigned work, claim a task before editing, implement, validate, commit, then submit to the Orchestrator for review. Run heavy commands and close reviews only in a Lane Manager slot. Don't restart the daemon or promote binaries.

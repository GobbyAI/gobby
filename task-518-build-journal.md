# Build Journal: Coordination Epic #518

Target epic: #513 (Gwiki Parity+ Roadmap)
Build command:

```bash
gobby build .gobby/plans/gwiki-parity-plus.md --isolation worktree --stage planning:max_review_rounds=99 --skip-stage pr --coordinator current
```

Created: 2026-06-07 20:03:50 CDT

## Startup Snapshot

- Coordination epic #518 created and claimed by session #1077.
- Target #513 build status before launch: never_started; no running agents; no active worktrees.
- Plan validation passed for `/Users/josh/Projects/gobby-cli/.gobby/plans/gwiki-parity-plus.md`.

## Build/Daemon Issues

### 2026-06-08T04:11:30Z — #552 worktree list visibility mismatch

- Symptom: `gobby-worktrees:list_worktrees` returned `count=0` for the active gobby-cli build project while `get_worktree_by_task` returned active worktrees for #513/#528/#530 and `get_worktree_stats(project_path=/Users/josh/Projects/gobby-cli)` reported five active worktrees.
- Where it surfaced: coordinator workspace/worktree-health sweep for target epic #513 during development after #530 entered QA review.
- Affected file/symbol: `src/gobby/mcp_proxy/tools/worktrees/_crud.py::list_worktrees`; regression coverage in `tests/mcp_proxy/tools/test_worktrees_lifecycle.py::test_list_worktrees_resolves_project_path`.
- Root cause: the list tool only accepted `project_id` or fell back to registry context, while stats already accepted `project_path` and resolved project context through `resolve_project_context`. Cross-project coordinator visibility could therefore diverge between stats/task lookups and list output.
- Action taken: committed `9031338cf` (`[gobby-#552] fix: resolve worktree list project context`) to add `project_path` support and project-context resolution parity for `list_worktrees`; focused validation passed with `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_worktrees_lifecycle.py -q`, `uv run ruff format --check src/gobby/mcp_proxy/tools/worktrees/_crud.py tests/mcp_proxy/tools/test_worktrees_lifecycle.py`, `uv run ruff check src/gobby/mcp_proxy/tools/worktrees/_crud.py tests/mcp_proxy/tools/test_worktrees_lifecycle.py`, and `uv run mypy src/gobby/mcp_proxy/tools/worktrees/_crud.py`.
- Restart gate: deferred because target #513 had active development agents `run-de3f57046a04` (#528) and `run-85065670ad75` (#532). Handoff: at the next quiet coordinator boundary, notify active agents if any, restart the daemon, verify `uv run gobby status`, call `gobby-sessions:compact_self`, rerun the full status sweep, and confirm `gobby-worktrees:list_worktrees` with `project_path=/Users/josh/Projects/gobby-cli` returns the active build worktrees.
- Linked task: #552.

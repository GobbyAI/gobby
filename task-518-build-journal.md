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

### 2026-06-08T01:09:10Z — #520 plan-file build created duplicate target #519

- Symptom: the required plan-file build command created duplicate target #519 (`Build gwiki-parity-plus.md`) while intended root epic #513 remained `never_started`. Planner runs `run-0dabb82eb1db` and `run-24077b4e790d` targeted #519 and failed in step-workflow setup.
- Where it surfaced: initial coordinator launch for `.gobby/plans/gwiki-parity-plus.md` before the real #513 lifecycle started.
- Affected file/symbol: `src/gobby/build/input_resolution.py::open_plan_file_build`; regression coverage in `tests/build_pipeline/test_service.py::test_build_plan_file_uses_registered_open_root_task`.
- Root cause: plan-file resolution only reused open roots that already had matching `task_artifacts.plan_file_path`; it did not consult the active plan registry row binding the plan path to root task #513.
- Action taken: committed `6d923aa9a` (`[gobby-#520] fix: reuse registered plan root task`) to resolve active registered roots before creating a new task, then committed `7316d9c7c` to close the validation gap around duplicate-target prevention and dispatch targeting. Focused validation passed for the new build-service regression and relevant lint/type checks. The wrong duplicate target #519 was disabled during recovery and later closed as obsolete once #513 completed.
- Restart gate: daemon restarted with `uv run gobby restart`; `uv run gobby status` reported Running before #513 automation resumed.
- Linked task: #520.

### 2026-06-08T01:47:10Z — #522 stale MCP wrapper blocked coordinator waits after restart

- Symptom: `gobby-agents:wait_for_agent` returned `GOBBY_MCP_WRAPPER_STALE` after a daemon/build-system restart, while other MCP status calls still worked. The coordinator had to fall back to manual status polling for agent completion.
- Where it surfaced: coordinator monitoring loop for #513 after the daemon restarted from the #520 fix.
- Affected files/symbols: `src/gobby/servers/routes/mcp/endpoints/execution.py`, MCP route tests in `tests/servers/test_mcp_routes.py`, and wrapper canonicalization coverage for wait-tool calls.
- Root cause: structured wait calls still depended on wrapper fingerprint freshness in paths used by long-lived coordinator sessions after daemon restart. Older wrappers could send stale or missing metadata even though the wait operation itself was otherwise safe to proxy.
- Action taken: committed `83d8b5b3a` and `eda9805ac` for #522 to recover coordinator-style wait calls with missing/stale wrapper metadata, add route-field canonicalization coverage, and preserve timeout caps. Focused validation passed for the MCP route/wrapper tests, Ruff format/check, mypy, and test-quality audit. A later live stdio recurrence was filed separately as #553 and fixed there.
- Restart gate: daemon restarted; `uv run gobby status` reported Running. Subsequent coordinator status calls worked, and the remaining live wait recurrence was tracked under #553 rather than folded into #522.
- Linked task: #522.

### 2026-06-08T04:11:30Z — #552 worktree list visibility mismatch

- Symptom: `gobby-worktrees:list_worktrees` returned `count=0` for the active gobby-cli build project while `get_worktree_by_task` returned active worktrees for #513/#528/#530 and `get_worktree_stats(project_path=/Users/josh/Projects/gobby-cli)` reported five active worktrees.
- Where it surfaced: coordinator workspace/worktree-health sweep for target epic #513 during development after #530 entered QA review.
- Affected file/symbol: `src/gobby/mcp_proxy/tools/worktrees/_crud.py::list_worktrees`; regression coverage in `tests/mcp_proxy/tools/test_worktrees_lifecycle.py::test_list_worktrees_resolves_project_path`.
- Root cause: the list tool only accepted `project_id` or fell back to registry context, while stats already accepted `project_path` and resolved project context through `resolve_project_context`. Cross-project coordinator visibility could therefore diverge between stats/task lookups and list output.
- Action taken: committed `9031338cf` (`[gobby-#552] fix: resolve worktree list project context`) to add `project_path` support and project-context resolution parity for `list_worktrees`; focused validation passed with `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_worktrees_lifecycle.py -q`, `uv run ruff format --check src/gobby/mcp_proxy/tools/worktrees/_crud.py tests/mcp_proxy/tools/test_worktrees_lifecycle.py`, `uv run ruff check src/gobby/mcp_proxy/tools/worktrees/_crud.py tests/mcp_proxy/tools/test_worktrees_lifecycle.py`, and `uv run mypy src/gobby/mcp_proxy/tools/worktrees/_crud.py`.
- Restart gate: deferred because target #513 had active development agents `run-de3f57046a04` (#528) and `run-85065670ad75` (#532). Handoff: at the next quiet coordinator boundary, notify active agents if any, restart the daemon, verify `uv run gobby status`, call `gobby-sessions:compact_self`, rerun the full status sweep, and confirm `gobby-worktrees:list_worktrees` with `project_path=/Users/josh/Projects/gobby-cli` returns the active build worktrees.
- Linked task: #552.

### 2026-06-08T04:18:49Z — #552 validation hardening

- Symptom: close validation rejected #552 because the first regression only asserted `project_path` resolution and did not prove list output stayed aligned with stats or task lookup visibility.
- Where it surfaced: `gobby-tasks:close_task` validation for #552 after commit `9031338cf`.
- Affected file/symbol: `tests/mcp_proxy/tools/test_worktrees_lifecycle.py::test_list_worktrees_resolves_project_path`.
- Root cause: the regression checked storage call arguments but did not exercise the coordinator-observed invariant across `list_worktrees`, `get_worktree_stats`, and `get_worktree_by_task`.
- Action taken: committed `3960b455e` (`[gobby-#552] test: cover worktree list consistency`) so the regression now verifies one resolved-project worktree is visible through list and task lookup, and that list count matches stats. Focused validation passed with `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_worktrees_lifecycle.py -q`, Ruff format/check, and targeted mypy on `_crud.py`.
- Restart gate: unchanged from the #552 fix entry; live daemon restart remains deferred until the next quiet coordinator boundary because active target build agents were still running.
- Linked task: #552.

### 2026-06-08T04:49:26Z — #553 stale MCP wait-tool recurrence

- Symptom: after #522, coordinator `gobby-agents:wait_for_agent` through the live `mcp__gobby.call_tool` path still returned `GOBBY_MCP_WRAPPER_STALE`, blocking bounded waits while ordinary MCP status calls worked.
- Where it surfaced: coordinator wait on target #513 after #528/#532 progressed and #529/#533 were active.
- Affected files/symbols: `src/gobby/mcp_proxy/stdio.py::DaemonProxy.call_tool`, `src/gobby/servers/routes/mcp/endpoints/execution.py::_stale_stdio_wrapper_wait_result`, `tests/mcp_proxy/test_mcp_proxy_stdio.py`, and `tests/servers/test_mcp_routes.py`.
- Root cause: the stdio wrapper still sent wait calls to the legacy `/api/mcp/{server}/tools/{tool}` route, where the daemon enforced wait-wrapper fingerprints. A daemon restart alone could not update already-running MCP stdio wrappers, and older wrappers might send stale or no fingerprint header.
- Action taken: committed `0537a6142` (`[gobby-#553] fix: route stdio wait calls through structured proxy`) and `aa42a8998` (`[gobby-#553] fix: tolerate stale wait wrapper fingerprints`). The first routes stdio wait calls through `/api/mcp/tools/call`; the second makes the daemon accept legacy wait-route calls from already-running wrappers so coordinator sessions recover without an MCP-server restart.
- Validation: `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/test_mcp_proxy_stdio.py::TestDaemonProxy::test_call_tool_uses_timeout_seconds_buffer_for_wait_tools tests/servers/test_mcp_routes.py::TestCallMCPTool::test_call_tool_ignores_explicit_stale_wait_wrapper_fingerprint tests/servers/test_mcp_routes.py::TestMCPProxy::test_proxy_accepts_missing_wait_wrapper_fingerprint tests/servers/test_mcp_routes.py::TestMCPProxy::test_proxy_accepts_stale_wait_wrapper_fingerprint tests/servers/test_mcp_routes.py::TestMCPProxy::test_proxy_accepts_current_wait_wrapper_fingerprint -q` passed. `uv run ruff format --check ...`, `uv run ruff check ...`, `uv run mypy src/gobby/mcp_proxy/stdio.py src/gobby/servers/routes/mcp/endpoints/execution.py`, and `uv run gobby test-quality audit tests/mcp_proxy/test_mcp_proxy_stdio.py tests/servers/test_mcp_routes.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` passed.
- Restart/live gate: active #513 agents were notified, daemon restarted, `uv run gobby status` reported Running, and a live `gobby-agents:wait_for_agent` call through `mcp__gobby.call_tool` returned `success=true` with `status=running` instead of `GOBBY_MCP_WRAPPER_STALE`.
- Linked task: #553.

### 2026-06-08T04:50:40Z — #552 restart gate completed

- Daemon restart was completed while target #513 had active agents #529 and #533; both received a build-scoped notice before restart.
- Post-restart verification: `uv run gobby status` reported Running. `gobby-worktrees:list_worktrees(project_path=/Users/josh/Projects/gobby-cli,status=active)` returned five active worktrees and `gobby-worktrees:get_worktree_stats(project_path=/Users/josh/Projects/gobby-cli)` returned `total=5`, `active=5`.
- Result: #552's deferred live gate is complete; worktree list visibility now matches stats for the active build project after restart.

# Gobby Build Journal

## 2026-05-31 02:33 CDT - Launch

- Coordinator session: `#6564`
- Coordination anchor epic: `#15385` in `/Users/josh/Projects/gobby`, intentionally left unclaimed per the run instructions.
- Target repo: `/Users/josh/Projects/gobby-cli`
- Target project: `gobby-cli`
- Plan: `/Users/josh/Projects/gobby-cli/.gobby/plans/gwiki-multimodal-ai.md`
- Requested build command:

```bash
uv run gobby build /Users/josh/Projects/gobby-cli/.gobby/plans/gwiki-multimodal-ai.md --project gobby-cli --coordinator current --isolation worktree --stage planning:max_review_rounds=99 --skip-stage pr
```

Initial setup note: the daemon skill memory referenced older anchor epic `#15277`, but the current run explicitly requested a new unclaimed anchor. I created `#15385` and will put any journal edits or build-system fixes under claimed leaf tasks beneath it.

## 2026-05-31 02:34 CDT - Build dispatched

The requested build command completed its initial dispatch successfully.

- Target build task: `#354` in `gobby-cli` (`caa08e59-25ad-4db1-86fe-d97830cd6b87`)
- Lifecycle: `planning -> expansion -> development -> holistic_qa -> merge`
- Skipped stage: `pr`
- Initial dispatcher tick: `scanned=2 executed=2 skipped=0`
- Current stage: `planning` is `in_progress`
- Active agent: planner `run-53b2c8840260`, child session `b77790c3-8717-4adb-a880-9f31bebb85b1`
- Planning review cap confirmed: `max_review_rounds=99`

No anomaly at launch. The shell did not expose `GOBBY_SESSION_ID`, so the launch set `GOBBY_SESSION_ID=8081ad75-d559-4a99-9a85-3af8c6904ca2` in the process environment to make `--coordinator current` resolve to this coordinator session.

## 2026-05-31 02:43 CDT - Agent telemetry counters were stale

While monitoring planner run `run-53b2c8840260`, `gobby-agents:list_running_agents` and `gobby-agents:wait_for_agent` reported `tool_calls_count=0` and `turns_used=0`. The child session terminal and transcript clearly showed many tool calls and assistant turns. That made the build look idle even though the planner was actively reading and revising the plan.

Opened build-system bug task `#15388` under coordination epic `#15385`. The fix teaches the agent MCP status tools to overlay live transcript activity from the active child session instead of relying only on stale aggregate session counters. It adds `TranscriptReader.get_activity_counts()`, passes the transcript reader into the agent registry, and applies the overlay to `list_running_agents`, `get_running_agent`, and `wait_for_agent`.

Validation run before commit:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_agent_live_stats.py -v` - passed, 5 tests
- `uv run ruff check src/gobby/mcp_proxy/tools/agent_live_activity.py src/gobby/mcp_proxy/tools/agents.py src/gobby/mcp_proxy/registries.py src/gobby/sessions/transcript_reader.py tests/mcp_proxy/tools/test_agent_live_stats.py` - passed
- `uv run ruff format --check src/gobby/mcp_proxy/tools/agent_live_activity.py src/gobby/mcp_proxy/tools/agents.py src/gobby/mcp_proxy/registries.py src/gobby/sessions/transcript_reader.py` - passed
- `uv run mypy src/gobby/mcp_proxy/tools/agent_live_activity.py src/gobby/mcp_proxy/tools/agents.py src/gobby/mcp_proxy/registries.py src/gobby/sessions/transcript_reader.py --no-incremental --strict` - passed
- `uv run gobby test-quality audit tests/mcp_proxy/tools/test_agent_live_stats.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` - passed

Resolution note: the running daemon still shows stale counters until it is restarted with this fix loaded.

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

## 2026-05-31 03:00 CDT - Coordinator messages could miss cross-project build agents

After committing the telemetry fix, I needed to warn the active planner agent before restarting the daemon. Sending a build-scoped message from this Gobby coordinator session to target build task `#354` in `gobby-cli` did not reach the child agent because the `gobby-agents:send_message` tool did not forward the target `project_id` into mailbox resolution. The same task number was interpreted in the coordinator's Gobby project instead of the target project. A direct agent message was also rejected as cross-project, even though this session is the recorded coordinator for the target build.

Opened build-system bug task `#15389` under coordination epic `#15385`. The fix adds an explicit `project_id` argument to `send_message`, forwards it to mailbox resolution, and allows cross-project build or direct agent messages only when build history proves the sender is the recorded coordinator for that target build. Ordinary cross-project session messages remain rejected.

Validation is starting now with focused messaging tests, lint, formatting checks, type checking, and test-quality audit.

Validation passed:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_agent_messaging.py -v` - passed, 31 tests
- `uv run ruff check src/gobby/sessions/mailbox.py src/gobby/mcp_proxy/tools/agent_messaging.py tests/mcp_proxy/tools/test_agent_messaging.py` - passed
- `uv run ruff format --check src/gobby/sessions/mailbox.py src/gobby/mcp_proxy/tools/agent_messaging.py tests/mcp_proxy/tools/test_agent_messaging.py` - passed
- `uv run mypy src/gobby/sessions/mailbox.py src/gobby/mcp_proxy/tools/agent_messaging.py --no-incremental --strict` - passed
- `uv run gobby test-quality audit tests/mcp_proxy/tools/test_agent_messaging.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` - passed
- `git diff --check` - passed

Post-restart validation found the first fix was incomplete. The daemon loaded the new schema and `send_message` advertised `project_id`, but a live build-scoped message with `target_id="#354"` and `project_id="gobby-cli"` still resolved `#354` in the coordinator's Gobby project and delivered to zero agents. I reopened and reclaimed bug task `#15389` to continue the fix instead of leaving the partial behavior in place.

Root cause: the MCP `call_tool` wrapper intentionally hoists `project_id` out of nested tool arguments and strips it before dispatch, so optional target-tool arguments named `project_id` do not receive it. The follow-up fix makes `send_message` use the wrapper's ambient project context when its direct `project_id` parameter is absent, and normalizes mailbox project references by resolving project names to ids before build and direct-recipient checks.

Follow-up validation passed:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/test_agent_messaging.py -v` - passed, 31 tests
- `uv run ruff check src/gobby/mcp_proxy/tools/agent_messaging.py src/gobby/sessions/mailbox.py tests/mcp_proxy/tools/test_agent_messaging.py` - passed
- `uv run ruff format --check src/gobby/mcp_proxy/tools/agent_messaging.py src/gobby/sessions/mailbox.py tests/mcp_proxy/tools/test_agent_messaging.py` - passed
- `uv run mypy src/gobby/mcp_proxy/tools/agent_messaging.py src/gobby/sessions/mailbox.py --no-incremental --strict` - passed
- `uv run gobby test-quality audit tests/mcp_proxy/tools/test_agent_messaging.py --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high` - passed
- `git diff --check` - passed

Committed the final follow-up fix as `943814fb2` and closed task `#15389`. I warned the active planner agent `run-5d365cd5edcd`, waited 30 seconds, restarted the daemon, and confirmed `uv run gobby status` showed the daemon healthy with automation running and one active agent. A live build-scoped message sent with wrapper `project_id=gobby-cli`, `target="build"`, and `target_id="#354"` resolved the target root task in `gobby-cli` (`caa08e59-25ad-4db1-86fe-d97830cd6b87`) and delivered/woke planner session `30512c29-8fda-42a7-95e5-c16775503cc6`. This resolved the cross-project coordinator messaging anomaly.

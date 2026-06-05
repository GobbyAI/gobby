# CodeRabbit Review Fixes Plan (#15679)

## Current Progress

- Task `#15679` is created and claimed for this session.
- CodeRabbit required skills are loaded: `coderabbit`, `review-learning`,
  `task-transitions`, `task-creation`, `development-discipline`.
- Review-learning recall has been run for all supplied findings.
- The finding table below has one row per supplied finding, including duplicate
  findings and stale/no-fix decisions.
- No source-code patch has landed yet; the first backend patch attempt failed
  during context matching and made no source changes.

## Test Judgment

This batch needs focused unit, CLI/API, websocket/session, and frontend validation. All pytest commands must be scoped and prefixed with `GOBBY_TEST_PROTECT=1`; do not run the full pytest suite.

## Finding Triage

| # | Decision | Path/File Name | Relevant Memory/Lesson | Reason/Planned Fix |
|---|---|---|---|---|
| 1 | fix | src/gobby/code_index/gcode_gateway.py | Codewiki JSON-body memories; no conflict | Remove redundant `await self._ensure_version()` from `codewiki`; keep `_run_json` unchanged. |
| 2 | fix | src/gobby/llm/sdk_utils.py | Context-budgeting memory; no conflict | Tighten `min_clean_cut` comment and return `text[:budget]` when breadcrumb leaves no head budget. |
| 3 | fix | src/gobby/review_learning/service.py | Review-learning memory; no conflict | Import `copy` and use `copy.deepcopy(finding)` in `_normalize_recall_findings`. |
| 4 | fix | src/gobby/sessions/token_usage.py | Gemini/provider memories; no conflict | Reword anomalous cache warning and add `action: will_clamp_later` to structured context. |
| 5 | fix | src/gobby/storage/sessions/_discovery.py | Session-context memories; no conflict | Strip `project_id` once and pass the normalized value to the query. |
| 6 | fix | src/gobby/tasks/expansion/_compile.py | Feature-routing memories; no conflict | Narrow LLM compile error wrapping to JSON/parsing errors and re-raise unexpected exceptions. |
| 7 | fix | tests/llm/test_llm_service.py, src/gobby/llm/service.py | Text-generation API memory; no conflict | Add public `text_generation` constructor injection and update tests to stop mutating `_text_generation`. |
| 8 | fix | tests/servers/websocket/chat/test_acp_plan_broadcast.py | Protected pytest and test-quality memories | Add `pytest` import, module unit marker, and async markers for async tests. |
| 9 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Add module `pytestmark` with `unit` and `asyncio`. |
| 10 | fix | tests/servers/websocket/chat/test_acp_plan_revise_loop.py | Protected pytest memory | Add `pytest` import and async/unit markers for the async revise-loop test. |
| 11 | fix | tests/servers/websocket/chat/test_managed_plan_broadcast.py | Protected pytest memory | Add unit module marker and async markers for async tests. |
| 12 | fix | tests/sessions/transcripts/test_grok_parser.py | No conflicting lesson | Add inline aggregation formula comment for input, cache-read, and cache-creation assertions. |
| 13 | fix | tests/test_mathutil.py | Package-layout and pytest marker memories | Import `add` from `gobby.mathutil`, import `pytest`, and mark `test_add` unit. |
| 14 | fix | src/gobby/llm/prompt_rendering.py | Jinja/template rendering memories | Catch Jinja `TemplateError` and Python-format `KeyError` or `ValueError`, raising descriptive `ValueError`. |
| 15 | fix | src/gobby/ai/text_generation.py | Text-generation telemetry memory | Include attempted JSON candidates and candidate errors in final JSON fallback `RuntimeError`. |
| 16 | fix | src/gobby/ai/text_generation.py | Text-generation telemetry memory | Include attempted text candidates and candidate errors in final text fallback `RuntimeError`. |
| 17 | fix | src/gobby/config/ai.py | No conflicting lesson | Simplify enabled local endpoint checks with direct truthiness and `.strip()`. |
| 18 | fix | src/gobby/mcp_proxy/tools/artifacts.py | Project-context memories | Replace generic project-path errors with missing context, invalid project path, offending file, and allowed roots. |
| 19 | fix | src/gobby/mcp_proxy/tools/sessions/_handoff.py | Session-summary feature-routing memory | Type `session_summary_config` as `SessionSummaryConfig` under `TYPE_CHECKING`. |
| 20 | fix | src/gobby/mcp_proxy/tools/wiki.py | No conflicting lesson | Replace `audit is True` with direct boolean use. |
| 21 | fix | src/gobby/memory/context.py | No conflicting lesson | Remove unreachable non-numeric score branch. |
| 22 | fix | src/gobby/memory/digest.py | Digest JSON contract memory | Keep return type coherent: document required non-None params and replace ambiguous `RuntimeError` with clear `TypeError`. |
| 23 | fix | src/gobby/servers/app_factory.py | Vite proxy memory; no conflict | Change nested `vite_proxy` return type from `Any` to `Response`. |
| 24 | fix | src/gobby/servers/session_changes.py | Session path memories; no conflict | Include `cwd` and `path` in unsafe path `ValueError`. |
| 25 | fix | src/gobby/servers/websocket/chat/backends/codex.py | Plan approval flow memory; no conflict | Return awaited `clear_session_context` result directly. |
| 26 | fix | src/gobby/servers/websocket/chat/backends/droid.py, src/gobby/servers/websocket/chat/backends/droid_stream.py, tests/servers/websocket/chat/test_droid_backend.py | Gcode/navigation memories | Rename exported Droid helpers to public names and update call sites/tests. |
| 27 | fix | src/gobby/sessions/transcript_index.py | Logging convention memory | Guard `len(value)` in non-serializable adjustment logging and fall back to `None`. |
| 28 | fix | src/gobby/tasks/expansion/_compile.py | Feature-routing memories | Duplicate of #6; same selective exception handling. |
| 29 | fix | src/gobby/workflows/summary_actions.py | Session-summary feature-routing memory | Add narrow `Protocol` for `.prompt` and replace `Any/None` annotation. |
| 30 | fix | tests/code_index/test_summarizer.py | Text-generation API memory | Use defensive prompt parsing with fallback name in `_SlowTextGenerateAdapter.generate`. |
| 31 | fix | tests/llm/test_llm_service.py | Python typing/no suppression memory | Add return type annotations to fake async generation methods. |
| 32 | fix | tests/mcp_proxy/tools/test_show_file.py | Test isolation memory | Create isolated project dir under `tmp_path`; keep outside file elsewhere under same tmp root. |
| 33 | fix | tests/review_learning/test_recall_context.py | Review-learning memory | Use `next(..., None)`, assert record exists, then assert fields. |
| 34 | fix | tests/servers/websocket/chat/test_acp_plan_broadcast.py | Protected pytest memory | Duplicate of #8; same module and async marker fix. |
| 35 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Duplicate of #9; module asyncio marker covers all async tests. |
| 36 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Duplicate of #9; module unit marker covers categorization. |
| 37 | fix | tests/test_mathutil.py | Package-layout and pytest marker memories | Duplicate of #13; same import and marker fix. |
| 38 | no-fix | web/src/components/chat/WikiChatActions.tsx, web/src/components/activity/WikiTab.tsx | Frontend accessibility memory; current code verified | Stale: `WikiChatActions` trigger already has `aria-label="Wiki actions"` and action buttons have visible labels. |
| 39 | fix | src/gobby/cli/projects.py | Project-verification memory | Type `ai_mode` as `Literal["auto", "on", "off"]` and remove the `type: ignore`. |
| 40 | no-fix | src/gobby/cli/sessions.py | Session-summary feature-routing memory | The finding is inaccurate: `create_llm_service` needs the full config. Rename the variable per #91 instead. |
| 41 | fix | src/gobby/config/feature_base.py | Feature candidate routing memory | Add legacy pre-validator for `provider`, `model`, and `tier`, removing legacy keys before `extra=forbid`. |
| 42 | fix | src/gobby/config/feature_base.py | Feature candidate routing memory | Reject candidates with missing provider/model around `/` before normalization. |
| 43 | fix | src/gobby/project_verification/candidates.py | Security/shell safety memory | Use `shlex.quote(subdir)` in package script command. |
| 44 | fix | src/gobby/project_verification/candidates.py | Project-verification memory | Detect unsafe tokens at start of command or after whitespace. |
| 45 | fix | src/gobby/servers/session_changes.py, tests/servers/test_session_changes.py | Session path memory | Allow `target == base` and add a unit test for equality. |
| 46 | fix | src/gobby/servers/websocket/chat/backends/droid.py | Gcode navigation memory | Exempt `is_gcode_shell_command(tool_input)` from Droid plan-mode Bash blocking. |
| 47 | fix | src/gobby/sessions/gzip_seek_index.py | Gzip transcript memory | Replace unreachable post-open `member is None` runtime check with an assertion. |
| 48 | fix | src/gobby/utils/project_init.py | Project-verification memory | Use `_optional_str` and one `custom` local in `VerificationCommands.from_dict`. |
| 49 | fix | tests/project_verification/test_refresh.py | Protected pytest memory | Add module-level `pytestmark = pytest.mark.unit`. |
| 50 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Duplicate of #9; module asyncio marker covers `test_sync_while_in_plan_mode_is_noop`. |
| 51 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Duplicate of #9; module asyncio marker covers approve test. |
| 52 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Duplicate of #9; module asyncio marker covers manual switch test. |
| 53 | fix | tests/servers/websocket/chat/test_acp_plan_revise_loop.py | Protected pytest memory | Duplicate of #10; async marker added. |
| 54 | fix | tests/servers/websocket/chat/test_stream_events.py | Test-quality memory | Remove redundant per-test unit decorators because module marker already applies. |
| 55 | fix | tests/test_mathutil.py | Package-layout and pytest marker memories | Duplicate of #13; same package import and unit marker. |
| 56 | skip | web/src/components/activity/PlanReviewCard.tsx | React plan-approval memory | Stale: current code has no local render-time approval state setters; status is derived from props. |
| 57 | skip | web/src/components/activity/PlanReviewCard.tsx | React plan-approval memory | Already satisfied: current code guards `plan.versions.length > 0` before indexing. |
| 58 | fix | web/src/components/chat/ChatInput.tsx | Frontend scroll memory | Reset textarea height to `auto` before measuring and keep cursor visible when clamped. |
| 59 | fix | web/src/components/chat/PlanPendingActionStrip.tsx | Accessibility/status memory | Add `role`, `aria-label`, and live semantics to pending strip. |
| 60 | fix | web/src/hooks/useChat/actions.ts | Plan approval flow memory | Return immediately for `keep_planning` after clearing approval UI. |
| 61 | fix | web/src/styles/base.css | Design-token memory | Combine duplicate `.source-icon-codex` and `.source-icon-grok` rules. |
| 62 | fix | src/gobby/cli/installers/git_hooks.py | Codewiki JSON-body and hook memories | Generate JSON with `jq` when available and keep a safe fallback for non-control-char paths. |
| 63 | fix | src/gobby/config/feature_base.py | Feature candidate routing memory | Tighten Claude alias matching to delimiter-bounded tokens, not substring containment. |
| 64 | fix | src/gobby/config/feature_candidate_defaults.py | Config/defaults memory | Make `_row_value` defensive for dict/list/tuple/malformed rows. |
| 65 | fix | src/gobby/config/feature_candidate_defaults.py | Config/defaults memory | Extract stale-key detection helper and simplify `stale_keys` comprehension. |
| 66 | fix | src/gobby/config/feature_candidate_defaults.py | Config/defaults memory | Narrow stale-default inspection catch to known database failures. |
| 67 | fix | src/gobby/hooks/event_handlers/_session_end.py | Session-end reason memory | Annotate `end_reason: SessionEndReason`. |
| 68 | fix | src/gobby/llm/prompt_rendering.py | Jinja/template rendering memories | Duplicate of #14; include renderer-specific failure context. |
| 69 | fix | src/gobby/mcp_proxy/tools/artifacts.py | Project-context memories | Duplicate of #18; include offending file and allowed roots. |
| 70 | fix | src/gobby/mcp_proxy/tools/wiki.py | No conflicting lesson | Duplicate of #20; direct boolean check. |
| 71 | fix | src/gobby/project_verification/refresh.py | Project-verification memory | Re-raise `MemoryError` in AI synthesis fallback handling. |
| 72 | fix | src/gobby/servers/routes/code_index.py | Codewiki JSON-body memory | Extract shared schedule-refresh exception logger and reuse it. |
| 73 | fix | src/gobby/servers/routes/code_index.py | Codewiki API memory | Type `CodewikiRefreshRequest.ai` as a `Literal` of accepted values. |
| 74 | fix | src/gobby/servers/session_changes.py | Session path memory | Duplicate of #45; allow base equality. |
| 75 | fix | src/gobby/servers/websocket/chat/permissions.py | Plan approval flow memory | Expand mixin class docstring with required attributes and invariants. |
| 76 | fix | src/gobby/sessions/summarize.py | Feature-routing memory | Replace `Any` feature config in protocol with a narrow protocol type. |
| 77 | fix | src/gobby/utils/project_init.py | Project-verification memory | Duplicate of #48; reduce repeated lookups. |
| 78 | fix | tests/cli/test_install_setup.py | Python typing memory | Add `-> None` to gwiki missing-binary test. |
| 79 | fix | tests/cli/test_projects_refresh_verification.py | Protected pytest memory | Type `monkeypatch: pytest.MonkeyPatch` on tests using it. |
| 80 | fix | tests/cli/test_projects_refresh_verification.py | Protected pytest memory | Add module-level unit marker. |
| 81 | fix | tests/memory/test_digest.py | Test typing memory | Replace `SimpleNamespace` helper with a typed dataclass holding `DigestConfig`. |
| 82 | fix | tests/servers/websocket/chat/test_acp_plan_broadcast.py | Protected pytest memory | Duplicate of #8; async markers. |
| 83 | fix | tests/servers/websocket/chat/test_acp_plan_mode_switch.py | Protected pytest memory | Duplicate of #9; module async marker. |
| 84 | fix | tests/servers/websocket/chat/test_acp_plan_revise_loop.py | Protected pytest memory | Duplicate of #10; async and unit markers. |
| 85 | fix | tests/servers/websocket/chat/test_managed_plan_broadcast.py | Protected pytest memory | Duplicate of #11; mark async tests. |
| 86 | fix | tests/servers/websocket/chat/test_plan_capability_flag.py | Protected pytest memory | Add module-level unit marker. |
| 87 | fix | tests/test_mathutil.py | Package-layout and pytest marker memories | Duplicate of #13; same marker. |
| 88 | skip | web/src/hooks/useChat/transportConversationEvents.ts | Frontend transport boundary memory | Already satisfied: current code defines `isApprovalOption` and filters `data.options`. |
| 89 | fix | src/gobby/ai/text_generation.py | Feature candidate routing memory | Change `_parse_candidate` to `rpartition("/")` and validate both parts. |
| 90 | fix | src/gobby/cli/installers/git_hooks.py | Codewiki JSON-body and hook memories | Duplicate of #62; handle control characters safely in fallback. |
| 91 | fix | src/gobby/cli/sessions.py | Session-summary feature-routing memory | Rename misleading `summary_config` local to `config` because it holds full app config. |
| 92 | fix | src/gobby/config/app.py | Config/defaults memory | Run stale-default cleanup before `config_store.get_all()`. |
| 93 | fix | src/gobby/config/feature_base.py | Feature candidate routing memory | Duplicate of #63; use delimiter-bounded Claude alias matching. |
| 94 | fix | src/gobby/hooks/dispatchers/mcp.py | Review-learning memory | Add concise `_is_review_lesson_memory` docstring. |
| 95 | fix | src/gobby/install/version_probe.py | No conflicting lesson | Remove redundant `is_absolute()` check after `resolve()`. |
| 96 | fix | src/gobby/llm/sdk_utils.py | Context-budgeting memory | Duplicate of #2; `head_budget <= 0` returns content, not clipped breadcrumb. |
| 97 | fix | src/gobby/mcp_proxy/importer.py | Feature candidate routing memory | Make `_claude_model_candidate` case-insensitive and raise `ValueError`. |
| 98 | fix | src/gobby/mcp_proxy/importer.py | Feature candidate routing memory | Validate `import_config.candidates` in `MCPServerImporter.__init__` for fail-fast clarity. |
| 99 | fix | src/gobby/memory/digest.py | Digest JSON contract memory | Duplicate of #22; use clear non-None contract and `TypeError`. |
| 100 | fix | src/gobby/project_verification/candidates.py | Project-verification memory | Replace `package_primary` double-negative with `is_custom` and preserve behavior. |
| 101 | fix | src/gobby/project_verification/evidence.py | Project-verification memory | Use `dataclasses.asdict` for nested dataclass serialization. |
| 102 | fix | src/gobby/project_verification/refresh.py | Logging convention memory | Log cleanup `OSError` around `os.unlink(tmp_name)` at debug. |
| 103 | no-fix | src/gobby/servers/routes/configuration_templates.py | Config alias export memory | Stale: defaults already call `model_dump(..., by_alias=True)`. |
| 104 | fix | src/gobby/servers/routes/mcp/endpoints/execution.py | Session context origin memory | Fix `session_ref_origin` ternary so header session id is explicit. |
| 105 | fix | src/gobby/servers/websocket/chat/backends/droid.py | Logging convention memory | Pipe Droid stderr and drain/log it in a background task. |
| 106 | fix | src/gobby/servers/websocket/chat/backends/droid.py | Streaming/error handling memory | Replace broad stream catch with targeted expected runtime/IO/connection exceptions. |
| 107 | fix | src/gobby/servers/websocket/handlers/plan_approval.py | Plan approval flow memory | Make `_inject_turn` return bool and update callers to observe failure. |
| 108 | fix | src/gobby/tasks/expansion/_compile.py | Feature-routing memory | Add concise docstrings/comments for `_expansion_feature_config` and `_model_for_provider`. |
| 109 | fix | src/gobby/workflows/summary_actions.py | Feature-routing memory | Duplicate of #29; use narrow protocol for `session_summary_config`. |
| 110 | fix | tests/cli/test_install_setup.py | Test naming memory | Rename gwiki test to describe missing-binary failure and keep assertions. |
| 111 | fix | tests/cli/test_projects_refresh_verification.py | Protected pytest memory | Expect `GOBBY_TEST_PROTECT=1 uv run pytest ...` in refreshed unit test command. |
| 112 | fix | tests/cli/test_projects_refresh_verification.py | Protected pytest memory | Duplicate of #79 and #80; marker and monkeypatch annotations. |
| 113 | fix | tests/memory/test_recall.py | Memory test helper memory | Preserve explicit empty tags with `tags if tags is not None else ["test"]`. |
| 114 | fix | tests/merge/test_resolver.py | Python typing memory | Add `Any` import and type annotations for `_StaticLLMService` methods. |
| 115 | fix | tests/servers/websocket/chat/test_acp_plan_broadcast.py | Protected pytest memory | Duplicate of #8; async markers. |
| 116 | fix | tests/servers/websocket/chat/test_acp_plan_revise_loop.py | Protected pytest memory | Duplicate of #10; add unit marker. |
| 117 | fix | tests/sessions/test_transcript_index.py | Gzip transcript memory | Add unit markers to gzip sidecar and nonserializable logging tests. |
| 118 | fix | tests/test_mathutil.py | Package-layout and pytest marker memories | Duplicate of #13; unit marker. |
| 119 | fix | web/src/components/chat/ChatInput.tsx | Frontend scroll memory | Duplicate of #58; always autoscroll when content exceeds max height. |
| 120 | fix | web/src/hooks/useChat/actions.ts | Plan approval flow memory | Duplicate of #60; return before approval send for keep-planning. |
| 121 | skip | web/src/hooks/useChat/transportConversationEvents.ts | Frontend transport boundary memory | Duplicate of #88; already satisfied by existing runtime option validation. |

## Implementation Order

1. Apply backend and config fixes first, keeping public behavior intact except for clearer validation/error reporting.
2. Apply test marker/helper fixes next so focused pytest can exercise the updated code paths.
3. Apply frontend fixes last, keeping `ChatInput.tsx` and `actions.ts` under 1,000 lines.
4. Run focused validation, fix any encountered failures, then commit with `[gobby-#15679]`.

## Validation Commands

- `GOBBY_TEST_PROTECT=1 uv run pytest` scoped to touched Python test files.
- `uv run ruff check` scoped to touched Python source and test files.
- `uv run mypy` scoped as narrowly as supported for touched modules if local config allows file-level runs.
- Frontend validation from `web/package.json` for touched TS/TSX/CSS files.

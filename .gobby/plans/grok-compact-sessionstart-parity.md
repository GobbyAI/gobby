# Grok Compact SessionStart Parity

**Plan ID:** grok-compact-sessionstart-parity

## Overview
`kind: framing`

Grok `/compact` is the same context loss Claude/Codex treat as `SessionStart(source=compact)`, but Grok never emits SessionStart. The live row keeps identity, claims, and variables; the model loses every Gobby injection and every “already shown” flag. This plan applies the compact-as-context-loss closeout on Grok `post_compact` and re-injects the same prompt-facing blocks on the next `turn_start`. Claude/Codex SessionStart behavior stays unchanged.

## Constraints
`kind: framing`

- Trigger stays Grok-only (`handle_post_compact` already special-cases Grok). Do not arm this path for Claude/Codex; they already SessionStart.
- Copy Claude compact **rehydration and tracking resets**. Do not copy SessionStart identity work (new row, parent expiry, agent re-activation, message-processor reregister).
- Do **not** run `reset-plan-mode-on-session-start`. Grok’s provider plan mode survives compact (`current_mode_update: plan` on #10529).
- Do **not** run `auto-run-pipeline` or `inject-previous-session-summary`.
- Do not inline the summary into the tmux continuation line.
- Do not add `post_compact` to `GROK_ADDITIONAL_CONTEXT_HOOKS`.
- Reuse `prepare_compact_continuation_variables`, `_reset_agent_context_injection`, and the existing inject templates. Same 6,500 / 9,950 budgets.
- `wait_for_summary` stays the continuation-prompt fallback.
- Keep every touched production file under 1,000 lines.

## P1: Compact-epoch closeout and rehydrate
`kind: framing`

**Goal**: After Grok compact, daemon tracking matches “context was lost” and the next user turn receives the same prompt-facing injections Claude/Codex get on compact SessionStart.

### 1.1 Apply compact-as-context-loss on Grok post_compact [category: code]
`kind: deliverable`

Targets:
- `src/gobby/sessions/compact_markers.py`
- `src/gobby/hooks/event_handlers/_session_start/in_place_compact.py`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::prepare_compact_continuation_variables`
- `src/gobby/hooks/event_handlers/_session_start/handoff.py::_variable_enabled`
- `src/gobby/hooks/event_handlers/_session_start/flow.py::_reset_agent_context_injection`
- `src/gobby/hooks/event_handlers/_misc.py::MiscEventHandlerMixin.handle_post_compact`
- `src/gobby/hooks/event_handlers/_session_responses.py::build_claimed_task_context`
- `tests/hooks/test_session_handoff_handlers.py::TestPrepareCompactContinuationVariables`
- `tests/hooks/test_session_handoff_handlers.py::TestCompactSelfContinuation.test_grok_post_compact_consumes_once_and_schedules_same_session`
- `tests/hooks/test_misc_handlers.py::TestPostCompactHandler`
- `tests/hooks/test_session_events_coverage.py::TestClaimedTaskHelpers`

Grok compact is explicit context loss (`compact` is in `_CONTEXT_LOSS_SOURCES`) without a SessionStart. A YAML-only inject is not enough: `handoff_summary_injectable` is only refreshed by `prepare_compact_continuation_variables`, and skill/schema/agent flags stay “already shown”.

Add `COMPACT_HANDOFF_INJECT_PENDING_VARIABLE = "compact_handoff_inject_pending"` in `compact_markers.py`.

Add a new module `src/gobby/hooks/event_handlers/_session_start/in_place_compact.py` (new file, no existing symbols) with `apply_in_place_compact_context_loss(handler, session_id) -> None`:

```python
def apply_in_place_compact_context_loss(handler: Any, session_id: str | None) -> None:
    prepare_compact_continuation_variables(handler, session_id, "compact")
    if not session_id or handler._session_manager is None:
        return

    from gobby.hooks.event_handlers._session_start.flow import (
        _reset_agent_context_injection,
    )
    from gobby.hooks.event_handlers._session_start.handoff import (
        _variable_enabled,
    )
    from gobby.sessions.compact_markers import COMPACT_HANDOFF_INJECT_PENDING_VARIABLE
    from gobby.workflows.state_manager import SessionVariableManager

    sv_mgr = SessionVariableManager(handler._session_manager.db)
    current = sv_mgr.get_variables(session_id)
    _reset_agent_context_injection(handler, session_id)

    updates: dict[str, Any] = {
        "unlocked_tools": [],
        "suggested_skill_names": [],
        "loaded_skills": [],
        "workflow_requested_skills": [],
        "memory_nudge_fired": False,
        "injected_memory_ids": [],
    }
    if _variable_enabled(current.get("auto_inject_handoff"), default=True):
        updates[COMPACT_HANDOFF_INJECT_PENDING_VARIABLE] = True
    sv_mgr.merge_variables(session_id, updates)

    session = handler._session_manager.get(session_id)
    project_id = getattr(session, "project_id", None) if session is not None else None
    if project_id:
        from gobby.hooks.event_handlers._session_responses import (
            build_claimed_task_context,
        )

        claimed = build_claimed_task_context(
            handler, session_id, project_id, compact=False
        )
        if claimed:
            sv_mgr.merge_variables(session_id, {"task_context": claimed})
```

This is the live-row equivalent of compact SessionStart rules `reset-progressive-discovery`, `reset-skill-injection`, `reset-memory-tracking-on-start`, plus `_reset_agent_context_injection` (SessionStart does this when `classify_session_start_context` returns `"full"` because `compact` is a context-loss source). `injected_memory_ids` is already cleared on `pre_compact`; reset again so the closeout is complete if `pre_compact` was skipped.

Do **not** write `plan_mode`, `mode_level`, `_assigned_pipeline`, or `skill_discovery_instructions_shown`.

When `auto_inject_handoff` is disabled, `prepare_compact_continuation_variables` already clears stale summary vars and consumes `handoff_source`. Still apply tracking resets and agent rehydrate (the model still lost schemas/skills/persona). Do **not** set `compact_handoff_inject_pending`.

Call the helper from the existing Grok-only branch in `handle_post_compact`, **before** `consume_and_schedule_compact_self_continuation`. Arm on every Grok PostCompact, including manual `/compact` with no `compact_self` marker. Continuation send failure must not skip the closeout.

```python
if source == "grok" and event.source is SessionSource.GROK:
    try:
        apply_in_place_compact_context_loss(self, session_id)
    except Exception:
        self.logger.warning(
            "POST_COMPACT: failed in-place compact context-loss closeout for session %s",
            session_id,
            exc_info=True,
        )
    try:
        consume_and_schedule_compact_self_continuation(...)
    except Exception:
        ...
```

Non-Grok `post_compact` stays occupancy-reset only.

Add closeout cases to `TestPrepareCompactContinuationVariables` (do not invent an unindexed test class):

- Summary present + auto-inject on → bounded `handoff_summary_injectable`, `handoff_source` gone, pending true, `unlocked_tools`/`loaded_skills`/`suggested_skill_names`/`workflow_requested_skills` empty, `memory_nudge_fired` false, `_agent_context_injected` false, `_agent_context_rehydrate_pending` true, `plan_mode` unchanged if it was true.
- Auto-inject disabled → pending not set, tracking resets and agent rehydrate still applied, summaries cleared.
- Claimed task → `task_context` refreshed via `build_claimed_task_context(..., compact=False)`.
- After closeout, `_inject_agent_instructions_if_needed` emits a preamble even when `_agent_context_injected` was previously true.

`TestPostCompactHandler` and `TestCompactSelfContinuation` cover the handler wiring:

- Non-Grok `handle_post_compact` → no pending, `handoff_source` preserved.
- Existing Grok continuation still schedules once.

**Acceptance:**

- 1.1.1 - Grok post_compact prepares handoff vars, consumes `handoff_source`, and arms `compact_handoff_inject_pending` when auto-inject is on. file: `src/gobby/hooks/event_handlers/_session_start/in_place_compact.py`.
- 1.1.2 - Closeout clears `unlocked_tools`, `loaded_skills`, `suggested_skill_names`, `workflow_requested_skills`, and `injected_memory_ids`. test: `tests/hooks/test_session_handoff_handlers.py::TestPrepareCompactContinuationVariables`.
- 1.1.3 - Closeout arms agent rehydrate so the next `before_agent` re-emits the preamble. symbol: `_reset_agent_context_injection`.
- 1.1.4 - `plan_mode` is left unchanged. test: `tests/hooks/test_session_handoff_handlers.py::TestPrepareCompactContinuationVariables`.
- 1.1.5 - Non-Grok post_compact does not apply the closeout. symbol: `MiscEventHandlerMixin.handle_post_compact`.
- 1.1.6 - Grok compact_self continuation still schedules once. test: `tests/hooks/test_session_handoff_handlers.py::TestCompactSelfContinuation.test_grok_post_compact_consumes_once_and_schedules_same_session`.

### 1.2 Re-inject compact SessionStart prompt context on the first turn_start [category: config] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml::*` — scope-reason: add sibling turn_start rule that re-injects compact SessionStart prompt blocks
- `tests/workflows/test_context_handoff_rules.py::TestContextHandoffSync`
- `tests/workflows/test_context_handoff_rules.py::TestInjectCompactHandoff`
- `src/gobby/install/bundled_content_manifest.json::*` — scope-reason: regenerate hashes after the bundled YAML edit

Grok `user_prompt_submit` is `BEFORE_AGENT`, which also maps to `turn_start`. That is the first hook that can deliver `additionalContext` after PostCompact (`ContextChannel.NONE` on Grok `post_compact`).

Keep `inject-compact-handoff` on `session_start` / `source == 'compact'` unchanged (Claude/Codex).

Add one sibling rule in the same YAML file. Duplicate the compact-handoff template, then append the wiki / profile / task sections that SessionStart would have injected. One rule, one `when`, so there is no priority race among four injects.

```yaml
  inject-compact-handoff-on-prompt:
    description: "Re-inject compact SessionStart context after in-place compaction"
    event: turn_start
    enabled: true
    priority: 11
    when: "variables.get('compact_handoff_inject_pending')"
    effects:
      - type: inject_context
        template: |
          <!-- gobby:injected-context:begin -->
          ## Continuation Context
          *Injected by Gobby session handoff*

          {{ handoff_summary_injectable or session_summary or '' }}
          {% set durable_mcp_calls = variables.get('mcp_calls') or {} %}
          {% if durable_mcp_calls %}

          ## Durable Tool-Call Evidence
          The daemon's per-session `mcp_calls` ledger durably recorded successful calls to the MCP tools below before this compaction. Pre-compaction turns are no longer visible in the transcript, so when a goal or stop condition asks whether one of these tools was invoked this session, treat this ledger as authoritative evidence that it was:

          {% for server, tools in durable_mcp_calls | dictsort %}- `{{ server }}`: {{ tools | sort | join(', ') }}
          {% endfor %}
          {% endif %}
          {% set resume_skills = variables.get('compact_resume_required_skills') or [] %}
          {% if resume_skills %}

          ## Required Skill Reload
          Reload these required skills with progressive discovery before continuing:

          {{ skill_fetch_batch_directive(resume_skills) }}
          {% endif %}
          {% set advisory_skills = variables.get('compact_resume_advisory_skills') or [] %}
          {% if advisory_skills %}

          ## Advisory Skill Reload
          Reload any of these with the same get_skill call only if they are still relevant to your remaining work (agent judgment):

          {% for skill in advisory_skills %}- `{{ skill }}`
          {% endfor %}
          {% endif %}
          {% if wiki_overview %}

          ## Project Wiki
          *Injected by Gobby wiki overview*

          {{ wiki_overview }}

          Query the wiki through the gobby-wiki MCP server (`wiki_search`, `wiki_ask`, `wiki_read`).
          {% endif %}
          {% if user_profile_content and not is_spawned_agent %}

          ## Global User Profile

          {{ user_profile_content }}
          {% endif %}
          {% if task_context %}

          {{ task_context }}
          {% endif %}
          <!-- gobby:injected-context:end -->
      - type: set_variable
        variable: compact_handoff_inject_pending
        value: false
      - type: set_variable
        variable: pending_context_reset
        value: false
      - type: set_variable
        variable: compact_resume_required_skills
        value: []
      - type: set_variable
        variable: compact_resume_advisory_skills
        value: []
```

`wiki_overview` and `user_profile_content` are already on the live row from the original SessionStart seeds. Do not re-seed them here. `task_context` is refreshed in 1.1 when a claim exists.

`pending_context_reset` is only cleared on SessionStart today. Clearing it here restores context-pressure nudges after Grok compact.

Add `inject-compact-handoff-on-prompt` to `CONTEXT_HANDOFF_RULES`.

Add the prompt-path cases to `TestInjectCompactHandoff` and the new name to `CONTEXT_HANDOFF_RULES` in `TestContextHandoffSync`:

- Synced event is `turn_start`, `when` is `compact_handoff_inject_pending`, template contains continuation markers, wiki/profile/task conditionals, and the injected-context sentinels.
- `RuleEngine.evaluate` on `BEFORE_AGENT` with pending true plus `session_summary`, `mcp_calls`, `wiki_overview`, `user_profile_content`, and `task_context` includes all of those sections.
- Spawned agent (`is_spawned_agent` true) omits the profile section.
- Pending false → no compact-handoff block.
- Second `BEFORE_AGENT` after the first does not re-inject.
- Existing SessionStart cases on the same class still pass.

Regenerate the bundled manifest:

```bash
uv run python -c "from pathlib import Path; from gobby.install.manifest import write_bundled_content_manifest; write_bundled_content_manifest(Path('src/gobby/install'))"
```

**Acceptance:**

- 1.2.1 - `inject-compact-handoff-on-prompt` is a `turn_start` rule gated on `compact_handoff_inject_pending`. file: `src/gobby/install/shared/workflows/rules/context-handoff/inject-compact-handoff.yaml`.
- 1.2.2 - First post-compact `BEFORE_AGENT` injects handoff, durable `mcp_calls`, skill reload, wiki, profile, and task when those vars are set. test: `tests/workflows/test_context_handoff_rules.py::TestInjectCompactHandoff`.
- 1.2.3 - Later turns do not re-inject after the one-shot clears. test: `tests/workflows/test_context_handoff_rules.py::TestInjectCompactHandoff`.
- 1.2.4 - SessionStart `inject-compact-handoff` is unchanged. test: `tests/workflows/test_context_handoff_rules.py::TestInjectCompactHandoff`.
- 1.2.5 - Bundled manifest hash is regenerated. file: `src/gobby/install/bundled_content_manifest.json`.

### 1.3 Document Grok compact SessionStart parity [category: docs] (depends: 1.2)
`kind: deliverable`

Targets:
- `docs/guides/variables.md`
- `docs/guides/sessions.md`

In `docs/guides/variables.md` document `compact_handoff_inject_pending`: one-shot bool, set by Grok `post_compact` closeout when auto-inject is on, cleared by `inject-compact-handoff-on-prompt`.

In `docs/guides/sessions.md` § Compaction Is In-Place, keep the Claude/Codex SessionStart paragraph. Append the Grok path:

- Grok compact does not emit SessionStart. `post_compact` runs `apply_in_place_compact_context_loss`: same summary prep and `handoff_source` consume as compact SessionStart, plus the compact context-loss tracking resets (`unlocked_tools`, skill-load lists, memory injection ids) and agent rehydrate.
- It does not reset `plan_mode` or fire pipelines.
- The next `turn_start` / `user_prompt_submit` injects the marked continuation block plus wiki / profile / task via `additionalContext`.
- `wait_for_summary` remains the fallback if that block is absent.

**Acceptance:**

- 1.3.1 - `compact_handoff_inject_pending` is documented. file: `docs/guides/variables.md`.
- 1.3.2 - Sessions guide states Grok post_compact closeout + turn_start inject, and that plan_mode is not reset. file: `docs/guides/sessions.md`.

## V2: Verification
`kind: verification`

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/hooks/test_session_handoff_handlers.py tests/hooks/test_misc_handlers.py tests/hooks/test_session_events_coverage.py tests/workflows/test_context_handoff_rules.py -v`
- Confirm Claude/Codex `session_start` + `source == compact` still injects only via `inject-compact-handoff`.
- Confirm Grok `post_compact` → `BEFORE_AGENT` injects the marked block once and rehydrates agent preamble.
- Confirm `plan_mode` is unchanged across the Grok closeout.
- No production file in this change reaches 1,000 lines.

## V1 Plan Changelog
`kind: framing`

Draft. Expanded from handoff-only inject to full compact SessionStart parity for Grok (tracking resets + wiki/profile/task + agent rehydrate). No adversary rounds yet.

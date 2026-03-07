"""Tests for agent-type-aware skill injection.

Exercises real SkillInjector, AgentContext, SkillProfile, and ParsedSkill
objects. No mocking needed except lightweight MagicMock stand-ins for
session/workflow_state objects passed to AgentContext.from_session().
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.skills.injector import (
    AgentContext,
    SkillInjector,
    SkillProfile,
    _derive_agent_type,
)
from gobby.skills.parser import ParsedSkill, SkillAudienceConfig

pytestmark = pytest.mark.unit


# =============================================================================
# _derive_agent_type
# =============================================================================


class TestDeriveAgentType:
    def test_depth_zero_is_interactive(self) -> None:
        assert _derive_agent_type(0, None) == "interactive"

    def test_depth_zero_ignores_workflow(self) -> None:
        assert _derive_agent_type(0, "worker") == "interactive"

    def test_depth_nonzero_no_workflow_is_autonomous(self) -> None:
        assert _derive_agent_type(1, None) == "autonomous"

    def test_orchestrator_keyword_orchestrate(self) -> None:
        assert _derive_agent_type(1, "orchestration-main") == "orchestrator"

    def test_orchestrator_keyword_orchestrator(self) -> None:
        assert _derive_agent_type(1, "my-orchestrator") == "orchestrator"

    def test_box_word_boundary_match(self) -> None:
        """'box' as a standalone word should match orchestrator pattern."""
        assert _derive_agent_type(1, "box-runner") == "orchestrator"
        assert _derive_agent_type(1, "my-box") == "orchestrator"

    def test_worker_keyword(self) -> None:
        assert _derive_agent_type(1, "meeseeks-worker") == "worker"

    def test_worker_exact(self) -> None:
        assert _derive_agent_type(1, "worker") == "worker"

    def test_no_match_is_autonomous(self) -> None:
        assert _derive_agent_type(2, "custom-flow") == "autonomous"

    def test_box_substring_no_match(self) -> None:
        """'box' as substring (sandbox) should NOT match."""
        assert _derive_agent_type(1, "sandbox-flow") == "autonomous"

    def test_worker_substring_no_match(self) -> None:
        """'worker' must be a word boundary match."""
        assert _derive_agent_type(1, "coworkers-flow") == "autonomous"

    def test_depth_two_no_workflow(self) -> None:
        assert _derive_agent_type(2, None) == "autonomous"

    def test_depth_three_with_orchestrator(self) -> None:
        assert _derive_agent_type(3, "orchestrator") == "orchestrator"


# =============================================================================
# AgentContext.from_session
# =============================================================================


class TestAgentContextFromSession:
    def test_minimal_session(self) -> None:
        session = MagicMock(agent_depth=0, source=None)
        ctx = AgentContext.from_session(session)
        assert ctx.agent_depth == 0
        assert ctx.has_human is True
        assert ctx.agent_type == "interactive"
        assert ctx.source is None
        assert ctx.workflow_name is None
        assert ctx.workflow_step is None
        assert ctx.task_type is None
        assert ctx.task_category is None

    def test_depth_sets_has_human_false(self) -> None:
        session = MagicMock(agent_depth=1, source=None)
        ctx = AgentContext.from_session(session)
        assert ctx.has_human is False
        assert ctx.agent_type == "autonomous"

    def test_source_enum_value(self) -> None:
        source = MagicMock()
        source.value = "claude"
        session = MagicMock(agent_depth=0, source=source)
        ctx = AgentContext.from_session(session)
        assert ctx.source == "claude"

    def test_source_string(self) -> None:
        session = MagicMock(agent_depth=0, source="gemini")
        ctx = AgentContext.from_session(session)
        assert ctx.source == "gemini"

    def test_workflow_state_extracts_name_and_step(self) -> None:
        session = MagicMock(agent_depth=0, source=None)
        wf = MagicMock(workflow_name="dev-flow", current_step="coding")
        ctx = AgentContext.from_session(session, workflow_state=wf)
        assert ctx.workflow_name == "dev-flow"
        assert ctx.workflow_step == "coding"

    def test_workflow_step_fallback_to_step_name(self) -> None:
        session = MagicMock(agent_depth=0, source=None)
        wf = MagicMock(workflow_name="step-flow", current_step=None, step_name="review")
        ctx = AgentContext.from_session(session, workflow_state=wf)
        assert ctx.workflow_step == "review"

    def test_workflow_state_none_step_and_step_name(self) -> None:
        """When both current_step and step_name are None, workflow_step stays None."""
        session = MagicMock(agent_depth=0, source=None)
        wf = MagicMock(workflow_name="flow", current_step=None, step_name=None)
        ctx = AgentContext.from_session(session, workflow_state=wf)
        assert ctx.workflow_step is None

    def test_task_extracts_type_and_category(self) -> None:
        session = MagicMock(agent_depth=0, source=None)
        task = {"task_type": "bug", "category": "code"}
        ctx = AgentContext.from_session(session, task=task)
        assert ctx.task_type == "bug"
        assert ctx.task_category == "code"

    def test_task_type_from_task_type_key(self) -> None:
        session = MagicMock(agent_depth=0, source=None)
        task = {"task_type": "feature"}
        ctx = AgentContext.from_session(session, task=task)
        assert ctx.task_type == "feature"

    def test_task_type_none_when_missing(self) -> None:
        """task_type is None when not present in task dict."""
        session = MagicMock(agent_depth=0, source=None)
        task = {"title": "some task"}
        ctx = AgentContext.from_session(session, task=task)
        assert ctx.task_type is None

    def test_none_depth_treated_as_zero(self) -> None:
        session = MagicMock(agent_depth=None, source=None)
        ctx = AgentContext.from_session(session)
        assert ctx.agent_depth == 0
        assert ctx.agent_type == "interactive"

    def test_no_workflow_state(self) -> None:
        """When workflow_state is None, workflow fields stay None."""
        session = MagicMock(agent_depth=0, source=None)
        ctx = AgentContext.from_session(session, workflow_state=None)
        assert ctx.workflow_name is None
        assert ctx.workflow_step is None

    def test_no_task(self) -> None:
        """When task is None, task fields stay None."""
        session = MagicMock(agent_depth=0, source=None)
        ctx = AgentContext.from_session(session, task=None)
        assert ctx.task_type is None
        assert ctx.task_category is None

    def test_full_context(self) -> None:
        """Build context with all fields populated."""
        source = MagicMock()
        source.value = "claude"
        session = MagicMock(agent_depth=1, source=source)
        wf = MagicMock(workflow_name="worker", current_step="execute")
        task = {"task_type": "bug", "category": "code"}
        ctx = AgentContext.from_session(session, workflow_state=wf, task=task)
        assert ctx.agent_depth == 1
        assert ctx.has_human is False
        assert ctx.agent_type == "worker"
        assert ctx.source == "claude"
        assert ctx.workflow_name == "worker"
        assert ctx.workflow_step == "execute"
        assert ctx.task_type == "bug"
        assert ctx.task_category == "code"

    def test_session_missing_agent_depth_attr(self) -> None:
        """Session with no agent_depth attribute defaults to 0."""
        session = MagicMock(spec=[])  # No attributes
        session.source = None
        ctx = AgentContext.from_session(session)
        assert ctx.agent_depth == 0


# =============================================================================
# AgentContext defaults
# =============================================================================


class TestAgentContextDefaults:
    def test_defaults(self) -> None:
        ctx = AgentContext()
        assert ctx.agent_depth == 0
        assert ctx.has_human is True
        assert ctx.workflow_name is None
        assert ctx.workflow_step is None
        assert ctx.task_type is None
        assert ctx.task_category is None
        assert ctx.agent_type == "interactive"
        assert ctx.source is None


# =============================================================================
# SkillProfile
# =============================================================================


class TestSkillProfile:
    def test_from_dict_full(self) -> None:
        data = {
            "audience": "worker",
            "include_skills": ["commit", "tasks"],
            "exclude_skills": ["debug"],
            "default_format": "full",
        }
        profile = SkillProfile.from_dict(data)
        assert profile.audience == "worker"
        assert profile.include_skills == ["commit", "tasks"]
        assert profile.exclude_skills == ["debug"]
        assert profile.default_format == "full"

    def test_from_dict_minimal(self) -> None:
        profile = SkillProfile.from_dict({})
        assert profile.audience is None
        assert profile.include_skills == []
        assert profile.exclude_skills == []
        assert profile.default_format is None

    def test_from_dict_partial(self) -> None:
        profile = SkillProfile.from_dict({"audience": "autonomous"})
        assert profile.audience == "autonomous"
        assert profile.include_skills == []
        assert profile.default_format is None

    def test_direct_construction(self) -> None:
        profile = SkillProfile(
            audience="orchestrator",
            include_skills=["deploy"],
            default_format="content",
        )
        assert profile.audience == "orchestrator"
        assert profile.include_skills == ["deploy"]
        assert profile.exclude_skills == []
        assert profile.default_format == "content"


# =============================================================================
# SkillInjector._matches_depth
# =============================================================================


class TestMatchesDepth:
    def setup_method(self) -> None:
        self.injector = SkillInjector()

    def test_int_exact_match(self) -> None:
        assert self.injector._matches_depth(0, 0) is True

    def test_int_no_match(self) -> None:
        assert self.injector._matches_depth(1, 0) is False

    def test_list_contains(self) -> None:
        assert self.injector._matches_depth([0, 1, 2], 1) is True

    def test_list_not_contains(self) -> None:
        assert self.injector._matches_depth([0, 1], 3) is False

    def test_list_empty(self) -> None:
        assert self.injector._matches_depth([], 0) is False

    def test_range_string_in_range(self) -> None:
        assert self.injector._matches_depth("0-2", 1) is True

    def test_range_string_at_low_boundary(self) -> None:
        assert self.injector._matches_depth("0-2", 0) is True

    def test_range_string_at_high_boundary(self) -> None:
        assert self.injector._matches_depth("0-2", 2) is True

    def test_range_string_outside(self) -> None:
        assert self.injector._matches_depth("0-2", 3) is False

    def test_range_string_below(self) -> None:
        assert self.injector._matches_depth("1-3", 0) is False

    def test_invalid_range_string_non_numeric(self) -> None:
        """Non-numeric range string falls through to warning + False."""
        assert self.injector._matches_depth("abc", 0) is False

    def test_invalid_range_string_non_numeric_with_dash(self) -> None:
        """'a-b' has two parts but int() raises ValueError, hitting except block."""
        assert self.injector._matches_depth("a-b", 0) is False

    def test_invalid_range_string_single_number(self) -> None:
        """Single number string (no dash) doesn't parse as range, falls through."""
        assert self.injector._matches_depth("5", 5) is False

    def test_invalid_range_string_too_many_parts(self) -> None:
        """'1-2-3' splits into 3 parts, len != 2, falls through."""
        assert self.injector._matches_depth("1-2-3", 1) is False

    def test_invalid_type_float(self) -> None:
        """Float is not int, list, or str -- hits the logger.warning fallthrough."""
        assert self.injector._matches_depth(3.14, 0) is False  # type: ignore[arg-type]

    def test_invalid_type_none(self) -> None:
        """None is not int, list, or str -- hits the logger.warning fallthrough."""
        assert self.injector._matches_depth(None, 0) is False  # type: ignore[arg-type]

    def test_invalid_type_dict(self) -> None:
        """Dict is not a valid depth_spec -- hits the logger.warning fallthrough."""
        assert self.injector._matches_depth({}, 0) is False  # type: ignore[arg-type]


# =============================================================================
# SkillInjector._matches_audience
# =============================================================================


class TestMatchesAudience:
    def setup_method(self) -> None:
        self.injector = SkillInjector()

    def test_audience_all_passes(self) -> None:
        config = SkillAudienceConfig(audience="all")
        ctx = AgentContext(agent_type="interactive")
        assert self.injector._matches_audience(config, ctx) is True

    def test_audience_all_passes_for_any_type(self) -> None:
        config = SkillAudienceConfig(audience="all")
        for agent_type in ["interactive", "autonomous", "orchestrator", "worker"]:
            ctx = AgentContext(agent_type=agent_type)
            assert self.injector._matches_audience(config, ctx) is True

    def test_audience_mismatch_fails(self) -> None:
        config = SkillAudienceConfig(audience="worker")
        ctx = AgentContext(agent_type="interactive")
        assert self.injector._matches_audience(config, ctx) is False

    def test_audience_match_passes(self) -> None:
        config = SkillAudienceConfig(audience="worker")
        ctx = AgentContext(agent_type="worker")
        assert self.injector._matches_audience(config, ctx) is True

    def test_depth_filter_matches(self) -> None:
        config = SkillAudienceConfig(audience="all", depth=0)
        ctx = AgentContext(agent_depth=0)
        assert self.injector._matches_audience(config, ctx) is True

    def test_depth_filter_rejects(self) -> None:
        config = SkillAudienceConfig(audience="all", depth=0)
        ctx = AgentContext(agent_depth=1)
        assert self.injector._matches_audience(config, ctx) is False

    def test_depth_none_passes_any(self) -> None:
        config = SkillAudienceConfig(audience="all", depth=None)
        ctx = AgentContext(agent_depth=5)
        assert self.injector._matches_audience(config, ctx) is True

    def test_steps_filter_match(self) -> None:
        config = SkillAudienceConfig(audience="all", steps=["coding", "review"])
        ctx = AgentContext(workflow_step="coding")
        assert self.injector._matches_audience(config, ctx) is True

    def test_steps_filter_no_match(self) -> None:
        config = SkillAudienceConfig(audience="all", steps=["coding"])
        ctx = AgentContext(workflow_step="review")
        assert self.injector._matches_audience(config, ctx) is False

    def test_steps_none_passes_any(self) -> None:
        config = SkillAudienceConfig(audience="all", steps=None)
        ctx = AgentContext(workflow_step="anything")
        assert self.injector._matches_audience(config, ctx) is True

    def test_task_categories_match(self) -> None:
        config = SkillAudienceConfig(audience="all", task_categories=["code", "test"])
        ctx = AgentContext(task_category="test")
        assert self.injector._matches_audience(config, ctx) is True

    def test_task_categories_no_match(self) -> None:
        config = SkillAudienceConfig(audience="all", task_categories=["code"])
        ctx = AgentContext(task_category="docs")
        assert self.injector._matches_audience(config, ctx) is False

    def test_task_categories_none_passes_any(self) -> None:
        config = SkillAudienceConfig(audience="all", task_categories=None)
        ctx = AgentContext(task_category="anything")
        assert self.injector._matches_audience(config, ctx) is True

    def test_sources_none_matches_any(self) -> None:
        config = SkillAudienceConfig(audience="all", sources=None)
        ctx = AgentContext(source="anything")
        assert self.injector._matches_audience(config, ctx) is True

    def test_sources_list_matches(self) -> None:
        config = SkillAudienceConfig(audience="all", sources=["claude_sdk_web_chat"])
        ctx = AgentContext(source="claude_sdk_web_chat")
        assert self.injector._matches_audience(config, ctx) is True

    def test_sources_list_rejects(self) -> None:
        config = SkillAudienceConfig(audience="all", sources=["claude_sdk_web_chat"])
        ctx = AgentContext(source="claude")
        assert self.injector._matches_audience(config, ctx) is False

    def test_sources_none_context_rejects(self) -> None:
        config = SkillAudienceConfig(audience="all", sources=["claude_sdk_web_chat"])
        ctx = AgentContext(source=None)
        assert self.injector._matches_audience(config, ctx) is False

    def test_all_filters_must_pass(self) -> None:
        """All conditions must pass -- audience match + depth match + step match."""
        config = SkillAudienceConfig(
            audience="worker",
            depth=1,
            steps=["execute"],
            task_categories=["code"],
        )
        ctx = AgentContext(
            agent_type="worker",
            agent_depth=1,
            workflow_step="execute",
            task_category="code",
        )
        assert self.injector._matches_audience(config, ctx) is True

    def test_audience_fails_short_circuits(self) -> None:
        """If audience doesn't match, other checks don't matter."""
        config = SkillAudienceConfig(
            audience="worker",
            depth=0,
            steps=["execute"],
        )
        ctx = AgentContext(
            agent_type="interactive",
            agent_depth=0,
            workflow_step="execute",
        )
        assert self.injector._matches_audience(config, ctx) is False


# =============================================================================
# SkillInjector._resolve_format
# =============================================================================


class TestResolveFormat:
    def setup_method(self) -> None:
        self.injector = SkillInjector()

    def _make_skill(
        self,
        injection_format: str = "summary",
        audience_config: SkillAudienceConfig | None = None,
    ) -> ParsedSkill:
        return ParsedSkill(
            name="test",
            description="test skill",
            content="body",
            injection_format=injection_format,
            audience_config=audience_config,
        )

    def test_default_format(self) -> None:
        skill = self._make_skill(injection_format="summary")
        ctx = AgentContext()
        assert self.injector._resolve_format(skill, ctx) == "summary"

    def test_profile_overrides_all(self) -> None:
        skill = self._make_skill(injection_format="summary")
        ctx = AgentContext()
        profile = SkillProfile(default_format="full")
        assert self.injector._resolve_format(skill, ctx, profile) == "full"

    def test_audience_format_override(self) -> None:
        config = SkillAudienceConfig(format_overrides={"autonomous": "content"})
        skill = self._make_skill(injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="autonomous")
        assert self.injector._resolve_format(skill, ctx) == "content"

    def test_audience_format_override_no_match(self) -> None:
        config = SkillAudienceConfig(format_overrides={"worker": "content"})
        skill = self._make_skill(injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        assert self.injector._resolve_format(skill, ctx) == "summary"

    def test_profile_beats_audience_override(self) -> None:
        config = SkillAudienceConfig(format_overrides={"interactive": "content"})
        skill = self._make_skill(injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        profile = SkillProfile(default_format="full")
        assert self.injector._resolve_format(skill, ctx, profile) == "full"

    def test_no_audience_config_uses_injection_format(self) -> None:
        skill = self._make_skill(injection_format="content")
        ctx = AgentContext()
        assert self.injector._resolve_format(skill, ctx) == "content"

    def test_audience_config_with_empty_format_overrides(self) -> None:
        config = SkillAudienceConfig(format_overrides={})
        skill = self._make_skill(injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        assert self.injector._resolve_format(skill, ctx) == "summary"

    def test_audience_config_with_none_format_overrides(self) -> None:
        config = SkillAudienceConfig(format_overrides=None)
        skill = self._make_skill(injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        assert self.injector._resolve_format(skill, ctx) == "summary"

    def test_profile_with_none_default_format(self) -> None:
        """Profile without default_format falls through to skill's format."""
        config = SkillAudienceConfig(format_overrides={"interactive": "content"})
        skill = self._make_skill(injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        profile = SkillProfile(default_format=None)
        assert self.injector._resolve_format(skill, ctx, profile) == "content"


# =============================================================================
# SkillInjector._should_include
# =============================================================================


class TestShouldInclude:
    def setup_method(self) -> None:
        self.injector = SkillInjector()

    def _make_skill(
        self,
        name: str = "test",
        always_apply: bool = False,
        audience_config: SkillAudienceConfig | None = None,
    ) -> ParsedSkill:
        return ParsedSkill(
            name=name,
            description=f"{name} desc",
            content="body",
            always_apply=always_apply,
            audience_config=audience_config,
        )

    def test_legacy_always_apply_true(self) -> None:
        skill = self._make_skill(always_apply=True)
        ctx = AgentContext()
        assert self.injector._should_include(skill, ctx) is True

    def test_legacy_always_apply_false(self) -> None:
        skill = self._make_skill(always_apply=False)
        ctx = AgentContext()
        assert self.injector._should_include(skill, ctx) is False

    def test_audience_config_overrides_always_apply(self) -> None:
        """When audience_config is present, always_apply is NOT used."""
        config = SkillAudienceConfig(audience="worker")
        skill = self._make_skill(always_apply=True, audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        assert self.injector._should_include(skill, ctx) is False

    def test_profile_exclude_takes_priority(self) -> None:
        skill = self._make_skill(name="debug", always_apply=True)
        ctx = AgentContext()
        profile = SkillProfile(exclude_skills=["debug"])
        assert self.injector._should_include(skill, ctx, profile) is False

    def test_profile_include_restricts(self) -> None:
        skill = self._make_skill(name="tasks", always_apply=True)
        ctx = AgentContext()
        profile = SkillProfile(include_skills=["commit"])
        assert self.injector._should_include(skill, ctx, profile) is False

    def test_profile_include_allows_listed(self) -> None:
        skill = self._make_skill(name="commit", always_apply=True)
        ctx = AgentContext()
        profile = SkillProfile(include_skills=["commit"])
        assert self.injector._should_include(skill, ctx, profile) is True

    def test_profile_exclude_empty_does_not_block(self) -> None:
        skill = self._make_skill(name="commit", always_apply=True)
        ctx = AgentContext()
        profile = SkillProfile(exclude_skills=[])
        assert self.injector._should_include(skill, ctx, profile) is True


# =============================================================================
# SkillInjector.select_skills
# =============================================================================


class TestSelectSkills:
    def setup_method(self) -> None:
        self.injector = SkillInjector()

    def _make_skill(
        self,
        name: str,
        always_apply: bool = False,
        audience_config: SkillAudienceConfig | None = None,
        injection_format: str = "summary",
    ) -> ParsedSkill:
        return ParsedSkill(
            name=name,
            description=f"{name} desc",
            content="body",
            always_apply=always_apply,
            injection_format=injection_format,
            audience_config=audience_config,
        )

    def test_legacy_always_apply(self) -> None:
        skill = self._make_skill("commit", always_apply=True)
        ctx = AgentContext()
        results = self.injector.select_skills([skill], ctx)
        assert len(results) == 1
        assert results[0][0].name == "commit"

    def test_legacy_not_always_apply_excluded(self) -> None:
        skill = self._make_skill("debug", always_apply=False)
        ctx = AgentContext()
        results = self.injector.select_skills([skill], ctx)
        assert len(results) == 0

    def test_audience_config_match(self) -> None:
        config = SkillAudienceConfig(audience="all", priority=10)
        skill = self._make_skill("tasks", audience_config=config)
        ctx = AgentContext()
        results = self.injector.select_skills([skill], ctx)
        assert len(results) == 1

    def test_audience_config_no_match(self) -> None:
        config = SkillAudienceConfig(audience="worker")
        skill = self._make_skill("tasks", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        results = self.injector.select_skills([skill], ctx)
        assert len(results) == 0

    def test_sorted_by_priority_then_name(self) -> None:
        s1 = self._make_skill("b-skill", audience_config=SkillAudienceConfig(priority=20))
        s2 = self._make_skill("a-skill", audience_config=SkillAudienceConfig(priority=10))
        s3 = self._make_skill("c-skill", audience_config=SkillAudienceConfig(priority=10))
        ctx = AgentContext()
        results = self.injector.select_skills([s1, s2, s3], ctx)
        names = [r[0].name for r in results]
        assert names == ["a-skill", "c-skill", "b-skill"]

    def test_profile_exclude(self) -> None:
        skill = self._make_skill("debug", always_apply=True)
        ctx = AgentContext()
        profile = SkillProfile(exclude_skills=["debug"])
        results = self.injector.select_skills([skill], ctx, profile)
        assert len(results) == 0

    def test_profile_include_only(self) -> None:
        s1 = self._make_skill("commit", always_apply=True)
        s2 = self._make_skill("tasks", always_apply=True)
        ctx = AgentContext()
        profile = SkillProfile(include_skills=["commit"])
        results = self.injector.select_skills([s1, s2], ctx, profile)
        assert len(results) == 1
        assert results[0][0].name == "commit"

    def test_empty_skills_list(self) -> None:
        ctx = AgentContext()
        results = self.injector.select_skills([], ctx)
        assert results == []

    def test_default_priority_for_no_audience_config(self) -> None:
        """Skills without audience_config get default priority of 50."""
        s1 = self._make_skill("legacy", always_apply=True)
        s2 = self._make_skill("new", audience_config=SkillAudienceConfig(priority=10))
        ctx = AgentContext()
        results = self.injector.select_skills([s1, s2], ctx)
        # s2 (priority 10) should come before s1 (priority 50)
        assert results[0][0].name == "new"
        assert results[1][0].name == "legacy"

    def test_format_resolution_in_results(self) -> None:
        """select_skills returns the resolved format for each skill."""
        config = SkillAudienceConfig(
            audience="all",
            format_overrides={"interactive": "content"},
        )
        skill = self._make_skill("tasks", injection_format="summary", audience_config=config)
        ctx = AgentContext(agent_type="interactive")
        results = self.injector.select_skills([skill], ctx)
        assert len(results) == 1
        assert results[0][1] == "content"

    def test_profile_format_in_results(self) -> None:
        """Profile default_format propagates to result tuples."""
        skill = self._make_skill("commit", always_apply=True, injection_format="summary")
        ctx = AgentContext()
        profile = SkillProfile(default_format="full")
        results = self.injector.select_skills([skill], ctx, profile)
        assert results[0][1] == "full"

    def test_mixed_skills_filtering(self) -> None:
        """Mix of legacy and audience-config skills, some matching some not."""
        s_legacy_yes = self._make_skill("legacy-yes", always_apply=True)
        s_legacy_no = self._make_skill("legacy-no", always_apply=False)
        s_audience_match = self._make_skill(
            "audience-match",
            audience_config=SkillAudienceConfig(audience="all"),
        )
        s_audience_reject = self._make_skill(
            "audience-reject",
            audience_config=SkillAudienceConfig(audience="worker"),
        )
        ctx = AgentContext(agent_type="interactive")
        results = self.injector.select_skills(
            [s_legacy_yes, s_legacy_no, s_audience_match, s_audience_reject],
            ctx,
        )
        names = [r[0].name for r in results]
        assert "legacy-yes" in names
        assert "legacy-no" not in names
        assert "audience-match" in names
        assert "audience-reject" not in names

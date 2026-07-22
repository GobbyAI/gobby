"""Tests for pipeline step rendering."""

import pytest

from gobby.workflows.definitions import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer, _filter_env
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit


def test_filter_env_excludes_sensitive_names_and_segments() -> None:
    env = {
        "PASSWORD": "exact-password",
        "token": "lowercase-token",
        "SECRET_STUFF": "prefixed-secret",
        "MY_SECRET_VALUE": "infix-secret",
        "mixed_ToKeN_value": "mixed-case-token",
        "GH_PAT": "token-alias",
        "AWS_SECRET_ACCESS_KEY": "known-sensitive-name",
        "database_url": "case-insensitive-sensitive-name",
        "PATH": "/usr/bin",
        "TOKENIZER_MODEL": "safe-tokenizer",
        "MONKEY": "safe-key-substring",
        "PUBLIC_URL": "https://example.test",
    }

    assert _filter_env(env) == {
        "PATH": "/usr/bin",
        "TOKENIZER_MODEL": "safe-tokenizer",
        "MONKEY": "safe-key-substring",
        "PUBLIC_URL": "https://example.test",
    }


def test_filter_env_explicit_allowlist_can_include_sensitive_names() -> None:
    env = {
        "PASSWORD": "allowed-secret",
        "PATH": "/usr/bin",
        "HOME": "/home/test",
    }

    assert _filter_env(env, frozenset({"PASSWORD", "PATH"})) == {
        "PASSWORD": "allowed-secret",
        "PATH": "/usr/bin",
    }


class TestRenderMcpArgumentsDropNone:
    """Unset optional inputs must be omitted from tool arguments, never sent as null.

    Regression for the review-pipeline spawn failure (#18717): inputs declared
    with default "" rendered to None and reached spawn_agent as null, which its
    schema rejects.
    """

    @pytest.fixture
    def renderer(self) -> StepRenderer:
        return StepRenderer(TemplateEngine())

    def test_render_step_omits_unset_optional_mcp_arguments(self, renderer: StepRenderer) -> None:
        step = PipelineStep(
            id="spawn_epic_reviewer",
            mcp=MCPStepConfig(
                server="gobby-agents",
                tool="spawn_agent",
                arguments={
                    "agent": "epic-reviewer",
                    "task_id": "${{ inputs.task_id }}",
                    "provider": "${{ inputs.provider }}",
                    "model": "${{ inputs.model }}",
                    "worktree_id": "${{ inputs.worktree_id }}",
                },
            ),
        )
        context = {
            "inputs": {"task_id": "#18705", "provider": "codex", "model": "", "worktree_id": ""},
            "steps": {},
        }

        rendered = renderer.render_step(step, context)

        assert rendered.mcp.arguments == {
            "agent": "epic-reviewer",
            "task_id": "#18705",
            "provider": "codex",
        }

    def test_drop_none_preserves_nested_payload_nulls(self, renderer: StepRenderer) -> None:
        args = {"unset": "${{ inputs.unset }}", "payload": {"keep_null": None, "value": 1}}
        rendered = renderer.render_mcp_arguments(
            args, {"inputs": {"unset": ""}, "steps": {}}, drop_none=True
        )

        assert rendered == {"payload": {"keep_null": None, "value": 1}}

    def test_drop_none_defaults_off(self, renderer: StepRenderer) -> None:
        rendered = renderer.render_mcp_arguments(
            {"unset": "${{ inputs.unset }}"}, {"inputs": {"unset": ""}, "steps": {}}
        )

        assert rendered == {"unset": None}

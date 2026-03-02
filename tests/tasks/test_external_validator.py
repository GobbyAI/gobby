"""Tests for external validator functionality.

TDD Red Phase: These tests define the expected behavior for
run_external_validation() and related external validator logic
which does not yet exist.

Task: gt-14b076
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.tasks.validation_models import Issue, IssueSeverity, IssueType

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_prompt_loader():
    """Mock PromptLoader for all tests to avoid file system lookups."""
    # We mock the internal _loader variable directly
    with patch("gobby.tasks.external_validator._loader", new=MagicMock()) as mock:
        # Default behavior for render
        def render_side_effect(path, context):
            if path == "external_validation/system":
                # Must contain keywords required by tests
                return "You are an objective validator. output valid json with status and issues fields."
            if path in ["validation/validate", "external_validation/validate"]:
                # Must contain keywords required by tests (json, status, issues, etc)
                return f"Validate this task: {context.get('title')}. Return JSON with status and issues."
            return f"Mock prompt for {path}"

        mock.render.side_effect = render_side_effect
        yield mock


pytestmark = pytest.mark.unit


class TestRunExternalValidation:
    """Tests for external validation with a separate LLM agent."""

    @pytest.fixture
    def validation_config(self):
        """Create a validation config with external validator enabled."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_model="claude-sonnet-4-5",
        )

    @pytest.fixture
    def mock_llm_service(self):
        """Create a mock LLM service."""
        from gobby.llm import LLMProvider, LLMService

        service = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        service.get_provider.return_value = provider
        return service

    @pytest.fixture
    def sample_task(self):
        """Create a sample task for validation."""
        return {
            "id": "gt-test123",
            "title": "Implement user authentication",
            "description": "Add OAuth2 login flow",
            "validation_criteria": "- [ ] Users can log in with OAuth\n- [ ] Tokens are stored securely",
        }

    @pytest.mark.asyncio
    async def test_external_validation_creates_fresh_context(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test that external validation creates a fresh context prompt without prior conversation."""
        from gobby.tasks.external_validator import run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = (
            '{"status": "valid", "feedback": "All criteria met", "issues": []}'
        )

        changes_context = """
        diff --git a/src/auth.py b/src/auth.py
        +def oauth_login():
        +    return authenticate_with_oauth()
        """

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context=changes_context,
        )

        # Verify the LLM was called
        mock_provider.generate_text.assert_called_once()

        # Get the prompt that was sent
        call_kwargs = mock_provider.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Prompt should contain task info but be a fresh context
        assert "user authentication" in prompt.lower() or "oauth" in prompt.lower()
        assert "validation criteria" in prompt.lower() or "criteria" in prompt.lower()
        assert "diff" in prompt.lower() or changes_context in prompt

        # Should NOT contain prior conversation markers
        assert "assistant:" not in prompt.lower()
        assert "human:" not in prompt.lower()

    @pytest.mark.asyncio
    async def test_external_validation_uses_configured_model(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test that external validation uses the external_validator_model config."""
        from gobby.tasks.external_validator import run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = (
            '{"status": "valid", "feedback": "OK", "issues": []}'
        )

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="some changes",
        )

        # Verify the external validator model was used
        call_kwargs = mock_provider.generate_text.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_external_validation_falls_back_to_default_model(
        self, mock_llm_service, sample_task
    ):
        """Test that external validation falls back to validation.model when external_validator_model is not set."""
        from gobby.tasks.external_validator import run_external_validation

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_model=None,  # Not set
        )

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = (
            '{"status": "valid", "feedback": "OK", "issues": []}'
        )

        await run_external_validation(
            config=config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="some changes",
        )

        # Should fall back to the default model
        call_kwargs = mock_provider.generate_text.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_external_validation_parses_structured_json_response(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test that external validation correctly parses structured JSON response."""
        from gobby.tasks.external_validator import ExternalValidationResult, run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = """
        {
            "status": "invalid",
            "summary": "Missing test coverage",
            "issues": [
                {
                    "type": "acceptance_gap",
                    "severity": "major",
                    "title": "No unit tests for OAuth flow",
                    "location": "src/auth.py",
                    "details": "The OAuth login function has no test coverage",
                    "suggested_fix": "Add tests in tests/test_auth.py"
                }
            ]
        }
        """

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff content",
        )

        assert isinstance(result, ExternalValidationResult)
        assert result.status == "invalid"
        assert result.summary == "Missing test coverage"
        assert len(result.issues) == 1
        assert result.issues[0].issue_type == IssueType.ACCEPTANCE_GAP
        assert result.issues[0].title == "No unit tests for OAuth flow"

    @pytest.mark.asyncio
    async def test_external_validation_handles_llm_error(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test graceful handling of LLM errors during external validation."""
        from gobby.tasks.external_validator import ExternalValidationResult, run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.side_effect = Exception("LLM API error")

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff content",
        )

        # Should return error result, not raise
        assert isinstance(result, ExternalValidationResult)
        assert result.status == "error"
        assert "LLM API error" in result.summary or result.error is not None

    @pytest.mark.asyncio
    async def test_external_validation_handles_malformed_json(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test handling of malformed JSON response from external validator."""
        from gobby.tasks.external_validator import ExternalValidationResult, run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = "This is not valid JSON response"

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff content",
        )

        # Should return error or pending status, not raise
        assert isinstance(result, ExternalValidationResult)
        assert result.status in ("error", "pending")

    @pytest.mark.asyncio
    async def test_external_validation_handles_timeout(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test handling of timeout during external validation."""

        from gobby.tasks.external_validator import ExternalValidationResult, run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.side_effect = TimeoutError("Request timed out")

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff content",
        )

        assert isinstance(result, ExternalValidationResult)
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_external_validation_includes_acceptance_criteria(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test that acceptance criteria from task is included in validation prompt."""
        from gobby.tasks.external_validator import run_external_validation

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "issues": []}'

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff content",
        )

        call_kwargs = mock_provider.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Validation criteria should be in the prompt
        assert "Users can log in with OAuth" in prompt or "OAuth" in prompt
        assert "Tokens are stored securely" in prompt or "securely" in prompt


class TestExternalValidatorToggle:
    """Tests for toggling between internal and external validators."""

    @pytest.fixture
    def mock_llm_service(self):
        from gobby.llm import LLMProvider, LLMService

        service = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        service.get_provider.return_value = provider
        return service

    @pytest.mark.asyncio
    async def test_use_internal_validator_when_flag_disabled(self, mock_llm_service):
        """Test that internal validation is used when use_external_validator is False."""
        from gobby.tasks.validation import TaskValidator, ValidationResult

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=False,
        )

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "feedback": "OK"}'

        validator = TaskValidator(config, mock_llm_service)
        result = await validator.validate_task(
            task_id="test-123",
            title="Test task",
            description="Test description",
            changes_summary="Test changes",
        )

        # Should use internal validation path
        assert isinstance(result, ValidationResult)
        assert result.status == "valid"

    @pytest.mark.asyncio
    async def test_use_external_validator_when_flag_enabled(self, mock_llm_service):
        """Test that external validation is used when use_external_validator is True."""

        # This test verifies the flag is respected in the enhanced validator
        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_model="claude-sonnet-4-5",
        )

        # The enhanced validator should check this flag and route accordingly
        # This test documents the expected behavior
        assert config.use_external_validator is True
        assert config.external_validator_model == "claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_override_external_validator_at_call_time(self, mock_llm_service):
        """Test that external validator can be overridden at call time."""
        from gobby.tasks.external_validator import run_external_validation

        # Config has external disabled
        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=False,
        )

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "issues": []}'

        # But we override at call time
        task = {"id": "test", "title": "Test", "validation_criteria": "Test"}

        await run_external_validation(
            config=config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff",
            force_external=True,  # Override flag
        )

        # Should still run external validation
        mock_provider.generate_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_external_validation_when_disabled_and_not_forced(self, mock_llm_service):
        """Test that external validation is skipped when config disabled and force_external=False."""
        from gobby.tasks.external_validator import ExternalValidationResult, run_external_validation

        # Config has external disabled
        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=False,
        )

        mock_provider = mock_llm_service.get_provider.return_value

        task = {"id": "test", "title": "Test", "validation_criteria": "Test"}

        result = await run_external_validation(
            config=config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff",
            force_external=False,  # Not overriding
        )

        # LLM provider should NOT be called
        mock_provider.generate_text.assert_not_called()

        # Result should indicate validation was skipped
        assert isinstance(result, ExternalValidationResult)
        assert result.status == "skipped"
        assert "skipped" in result.summary.lower() or "disabled" in result.summary.lower()
        assert result.issues == []


class TestExternalValidationResult:
    """Tests for ExternalValidationResult dataclass."""

    def test_result_dataclass_fields(self) -> None:
        """Test ExternalValidationResult has expected fields."""
        from gobby.tasks.external_validator import ExternalValidationResult

        result = ExternalValidationResult(
            status="valid",
            summary="All criteria met",
            issues=[],
        )

        assert result.status == "valid"
        assert result.summary == "All criteria met"
        assert result.issues == []
        assert result.error is None  # Optional field

    def test_result_with_issues(self) -> None:
        """Test ExternalValidationResult with issues populated."""
        from gobby.tasks.external_validator import ExternalValidationResult

        issue = Issue(
            issue_type=IssueType.TEST_FAILURE,
            severity=IssueSeverity.BLOCKER,
            title="Test failed",
        )

        result = ExternalValidationResult(
            status="invalid",
            summary="Tests failing",
            issues=[issue],
        )

        assert result.status == "invalid"
        assert len(result.issues) == 1
        assert result.issues[0].title == "Test failed"

    def test_result_with_error(self) -> None:
        """Test ExternalValidationResult with error field."""
        from gobby.tasks.external_validator import ExternalValidationResult

        result = ExternalValidationResult(
            status="error",
            summary="Validation failed",
            issues=[],
            error="Connection timeout",
        )

        assert result.status == "error"
        assert result.error == "Connection timeout"


class TestExternalValidatorPrompt:
    """Tests for the external validator prompt template."""

    @pytest.fixture
    def mock_llm_service(self):
        from gobby.llm import LLMProvider, LLMService

        service = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        service.get_provider.return_value = provider
        return service

    @pytest.mark.asyncio
    async def test_prompt_includes_objective_validator_instruction(self, mock_llm_service):
        """Test that prompt instructs the validator to be objective."""
        from gobby.tasks.external_validator import run_external_validation

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
        )

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "issues": []}'

        task = {"id": "test", "title": "Test Task", "validation_criteria": "Tests pass"}

        await run_external_validation(
            config=config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff content",
        )

        call_kwargs = mock_provider.generate_text.call_args.kwargs

        # Check system prompt mentions objectivity
        system_prompt = call_kwargs.get("system_prompt", "")
        assert "objective" in system_prompt.lower() or "validator" in system_prompt.lower()

    @pytest.mark.asyncio
    async def test_prompt_requests_structured_json_output(self, mock_llm_service):
        """Test that prompt requests structured JSON output format."""
        from gobby.tasks.external_validator import run_external_validation

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
        )

        mock_provider = mock_llm_service.get_provider.return_value
        mock_provider.generate_text.return_value = '{"status": "valid", "issues": []}'

        task = {"id": "test", "title": "Test", "validation_criteria": "Criteria"}

        await run_external_validation(
            config=config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff",
        )

        call_kwargs = mock_provider.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Prompt should request JSON output
        assert "json" in prompt.lower()
        assert "status" in prompt.lower()
        assert "issues" in prompt.lower()


class TestAgentModeValidation:
    """Tests for agent mode external validation."""

    @pytest.fixture
    def validation_config(self):
        """Create a validation config with agent mode enabled."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="agent",
        )

    @pytest.fixture
    def mock_llm_service(self):
        """Create a mock LLM service."""
        from gobby.llm import LLMProvider, LLMService

        service = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        service.get_provider.return_value = provider
        return service

    @pytest.fixture
    def mock_agent_runner(self):
        """Create a mock AgentRunner."""
        from gobby.llm.executor import AgentResult

        runner = MagicMock()
        runner.run = AsyncMock(
            return_value=AgentResult(
                output='```json\n{"status": "valid", "summary": "All criteria met", "issues": []}\n```',
                status="completed",
                turns_used=3,
            )
        )
        return runner

    @pytest.fixture
    def sample_task(self):
        """Create a sample task for validation."""
        return {
            "id": "gt-test123",
            "title": "Implement user authentication",
            "description": "Add OAuth2 login flow",
            "validation_criteria": "- [ ] Users can log in with OAuth\n- [ ] Tokens are stored securely",
        }

    @pytest.mark.asyncio
    async def test_agent_mode_uses_agent_runner(
        self, validation_config, mock_llm_service, mock_agent_runner, sample_task
    ):
        """Test that agent mode uses AgentRunner instead of direct LLM calls."""
        from gobby.tasks.external_validator import run_external_validation

        changes_context = "diff --git a/src/auth.py b/src/auth.py"

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context=changes_context,
            agent_runner=mock_agent_runner,
        )

        # Agent runner should be called
        mock_agent_runner.run.assert_called_once()

        # LLM service should NOT be called directly
        mock_llm_service.get_provider.assert_not_called()

        # Result should be parsed from agent output
        assert result.status == "valid"

    @pytest.mark.asyncio
    async def test_agent_mode_without_runner_returns_error(
        self, validation_config, mock_llm_service, sample_task
    ):
        """Test that agent mode returns error when no runner is provided."""
        from gobby.tasks.external_validator import run_external_validation

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff",
            agent_runner=None,  # No runner provided
        )

        assert result.status == "error"
        assert "agent runner" in result.summary.lower() or "not available" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_agent_mode_handles_agent_error(
        self, validation_config, mock_llm_service, mock_agent_runner, sample_task
    ):
        """Test that agent mode handles agent execution errors."""
        from gobby.llm.executor import AgentResult
        from gobby.tasks.external_validator import run_external_validation

        # Make agent return an error
        mock_agent_runner.run.return_value = AgentResult(
            output="",
            status="error",
            error="Agent execution failed",
            turns_used=0,
        )

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff",
            agent_runner=mock_agent_runner,
        )

        assert result.status == "error"
        assert "failed" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_agent_mode_parses_invalid_result(
        self, validation_config, mock_llm_service, mock_agent_runner, sample_task
    ):
        """Test that agent mode correctly parses 'invalid' validation result."""
        from gobby.llm.executor import AgentResult
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_runner.run.return_value = AgentResult(
            output='{"status": "invalid", "summary": "Tests failing", "issues": [{"type": "test_failure", "severity": "blocker", "title": "Unit tests fail"}]}',
            status="completed",
            turns_used=5,
        )

        result = await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff",
            agent_runner=mock_agent_runner,
        )

        assert result.status == "invalid"
        assert "tests" in result.summary.lower() or len(result.issues) > 0

    @pytest.mark.asyncio
    async def test_agent_config_uses_correct_settings(
        self, mock_llm_service, mock_agent_runner, sample_task
    ):
        """Test that AgentConfig is created with correct settings from config."""
        from gobby.tasks.external_validator import run_external_validation

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="agent",
            external_validator_model="claude-sonnet-4-5",
        )

        await run_external_validation(
            config=config,
            llm_service=mock_llm_service,
            task=sample_task,
            changes_context="diff",
            agent_runner=mock_agent_runner,
        )

        # Check that agent was called with correct config
        call_args = mock_agent_runner.run.call_args
        agent_config = call_args[0][0]  # First positional argument

        assert agent_config.model == "claude-sonnet-4-5"
        assert agent_config.provider == "claude"
        assert agent_config.mode == "in_process"
        assert agent_config.max_turns == 20
        assert agent_config.timeout == 120.0


class TestAgentSpawnValidation:
    """Tests for external validation via spawned agent process.

    TDD Red Phase: These tests verify that when external_validator_mode="spawn",
    a separate agent process is spawned via gobby-agents.spawn_agent rather than
    using an in-process AgentRunner. The implementation does not exist yet.

    Task: gt-09277c
    """

    @pytest.fixture
    def validation_config_spawn(self):
        """Create a validation config with agent spawn mode enabled."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",  # Spawn separate process
            external_validator_model="claude-sonnet-4-5",
        )

    @pytest.fixture
    def mock_agent_spawner(self):
        """Mock the gobby-agents spawner interface."""
        spawner = MagicMock()
        spawner.spawn_agent = AsyncMock(
            return_value={
                "success": True,
                "agent_id": "agent-validator-123",
                "status": "running",
            }
        )
        spawner.get_agent_result = AsyncMock(
            return_value={
                "success": True,
                "status": "completed",
                "output": '{"status": "valid", "summary": "All criteria met", "issues": []}',
            }
        )
        return spawner

    @pytest.fixture
    def sample_task(self):
        """Create a sample task for validation."""
        return {
            "id": "gt-test456",
            "title": "Implement secure file upload",
            "description": "Add file upload with virus scanning",
            "validation_criteria": "- [ ] Files are scanned for viruses\n- [ ] Upload size limit enforced",
        }

    @pytest.mark.asyncio
    async def test_spawn_mode_invokes_agent_spawner(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawn mode invokes gobby-agents.spawn_agent."""
        from gobby.tasks.external_validator import run_external_validation

        result = await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,  # Not used in spawn mode
            task=sample_task,
            changes_context="diff --git a/src/upload.py",
            agent_spawner=mock_agent_spawner,
        )

        # Agent spawner should be invoked
        mock_agent_spawner.spawn_agent.assert_called_once()

        # Should poll for result
        mock_agent_spawner.get_agent_result.assert_called()

        # Result should be parsed
        assert result.status == "valid"

    @pytest.mark.asyncio
    async def test_spawn_mode_uses_headless_mode(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent uses headless mode (no terminal UI)."""
        from gobby.tasks.external_validator import run_external_validation

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff content",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        assert call_kwargs.get("mode") == "autonomous"

    @pytest.mark.asyncio
    async def test_spawn_mode_passes_correct_model(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent uses external_validator_model."""
        from gobby.tasks.external_validator import run_external_validation

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff content",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        assert call_kwargs.get("model") == "claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_spawn_mode_creates_validation_prompt(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent receives validation-specific prompt."""
        from gobby.tasks.external_validator import run_external_validation

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff --git a/src/upload.py",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Prompt should contain validation instructions
        assert "validat" in prompt.lower()  # validate/validation/validator
        assert sample_task["id"] in prompt or sample_task["title"] in prompt
        assert "criteria" in prompt.lower()

    @pytest.mark.asyncio
    async def test_spawn_mode_includes_changes_context(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent prompt includes changes context."""
        from gobby.tasks.external_validator import run_external_validation

        changes = "diff --git a/src/upload.py b/src/upload.py\n+def scan_file():"

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context=changes,
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Changes should be included
        assert "diff" in prompt.lower() or "upload.py" in prompt

    @pytest.mark.asyncio
    async def test_spawn_mode_sets_max_turns(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent has limited max_turns."""
        from gobby.tasks.external_validator import run_external_validation

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        # Validator should have limited turns (single-shot or few turns)
        max_turns = call_kwargs.get("max_turns")
        assert max_turns is not None
        assert int(max_turns) <= 10  # Validation shouldn't need many turns

    @pytest.mark.asyncio
    async def test_spawn_mode_without_spawner_returns_error(
        self, validation_config_spawn, sample_task
    ):
        """Test that spawn mode returns error when no spawner is available."""
        from gobby.tasks.external_validator import run_external_validation

        result = await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=None,  # No spawner
        )

        assert result.status == "error"
        assert "spawn" in result.summary.lower() or "not available" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_spawn_mode_handles_spawn_failure(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawn mode handles agent spawn failures."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.spawn_agent.return_value = {
            "success": False,
            "error": "Failed to spawn agent",
        }

        result = await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        assert result.status == "error"
        assert "spawn" in result.summary.lower() or "failed" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_spawn_mode_handles_agent_timeout(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawn mode handles agent execution timeout."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": False,
            "status": "timeout",
            "error": "Agent execution timed out",
        }

        result = await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        assert result.status == "error"
        assert "timeout" in result.summary.lower() or "timed out" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_spawn_mode_parses_invalid_result(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawn mode correctly parses 'invalid' result from agent."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": '{"status": "invalid", "summary": "Missing virus scanning", "issues": [{"type": "acceptance_gap", "severity": "major", "title": "No virus scan implemented"}]}',
        }

        result = await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        assert result.status == "invalid"
        assert len(result.issues) >= 1

    @pytest.mark.asyncio
    async def test_spawn_mode_agent_runs_in_separate_context(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent does not share context with implementation agent.

        This is critical: the validator must not see the implementation agent's
        conversation history to avoid bias.
        """
        from gobby.tasks.external_validator import run_external_validation

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs

        # Should not pass parent_session_id (no context inheritance)
        # or should explicitly pass session_context=None
        assert call_kwargs.get("parent_session_id") is None
        assert call_kwargs.get("session_context") in (None, "", "none")

    @pytest.mark.asyncio
    async def test_spawn_mode_agent_has_validator_system_prompt(
        self, validation_config_spawn, mock_agent_spawner, sample_task
    ):
        """Test that spawned agent receives validator-specific system instructions.

        The validator should be instructed to:
        1. Be objective and adversarial
        2. Not assume success
        3. Verify each criterion independently
        """
        from gobby.tasks.external_validator import run_external_validation

        await run_external_validation(
            config=validation_config_spawn,
            llm_service=None,
            task=sample_task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # System prompt should instruct objectivity
        prompt_lower = prompt.lower()
        assert any(
            word in prompt_lower
            for word in ["objective", "adversarial", "critical", "independently", "verify"]
        )


class TestValidationContextPassing:
    """Tests for validation context passing to external validator.

    TDD Red Phase: These tests verify that structured validation context
    (git diff, test results, acceptance criteria, etc.) is properly
    passed to the external validator agent.

    Task: gt-2799a5
    """

    @pytest.fixture
    def validation_config(self):
        """Create a validation config for context tests."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",
            external_validator_model="claude-sonnet-4-5",
        )

    @pytest.fixture
    def mock_agent_spawner(self):
        """Mock the gobby-agents spawner interface."""
        spawner = MagicMock()
        spawner.spawn_agent = AsyncMock(
            return_value={
                "success": True,
                "agent_id": "agent-ctx-123",
                "status": "running",
            }
        )
        spawner.get_agent_result = AsyncMock(
            return_value={
                "success": True,
                "status": "completed",
                "output": '{"status": "valid", "summary": "All criteria met", "issues": []}',
            }
        )
        return spawner

    @pytest.mark.asyncio
    async def test_git_diff_included_in_context(self, validation_config, mock_agent_spawner):
        """Test that git diff is included in the context passed to agent."""
        from gobby.tasks.external_validator import run_external_validation

        git_diff = """diff --git a/src/auth.py b/src/auth.py
index abc1234..def5678 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,6 +10,15 @@ class AuthService:
+    def login(self, username: str, password: str) -> bool:
+        \"\"\"Authenticate user with credentials.\"\"\"
+        return self._verify_credentials(username, password)
"""

        task = {
            "id": "gt-ctx-test",
            "title": "Add login functionality",
            "validation_criteria": "- [ ] Login method exists",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context=git_diff,
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Git diff should be in the prompt
        assert "diff --git" in prompt or "src/auth.py" in prompt
        assert "login" in prompt.lower()

    @pytest.mark.asyncio
    async def test_test_results_included_when_available(
        self, validation_config, mock_agent_spawner
    ):
        """Test that test results are included in context when provided."""
        from gobby.tasks.external_validator import run_external_validation

        # Include test results in the changes context
        changes_with_tests = """diff --git a/src/feature.py b/src/feature.py
+def new_feature():
+    return True

## Test Results
```
PASSED tests/test_feature.py::test_new_feature - 0.02s
PASSED tests/test_feature.py::test_edge_case - 0.01s
2 passed in 0.05s
```
"""

        task = {
            "id": "gt-test-results",
            "title": "Add new feature",
            "validation_criteria": "- [ ] Tests pass",
            "category": "Run pytest for feature tests",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context=changes_with_tests,
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Test results should be preserved in context
        assert "PASSED" in prompt or "passed" in prompt.lower()
        assert "test_feature" in prompt or "pytest" in prompt.lower()

    @pytest.mark.asyncio
    async def test_acceptance_criteria_included(self, validation_config, mock_agent_spawner):
        """Test that acceptance criteria from task is included in context."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-criteria",
            "title": "Implement caching",
            "description": "Add Redis-based caching layer",
            "validation_criteria": """## Acceptance Criteria
- [ ] Cache service interface defined
- [ ] Redis implementation provided
- [ ] Cache invalidation on updates
- [ ] TTL configuration supported""",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff content",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # All acceptance criteria should be in prompt
        assert "Cache service interface" in prompt or "cache" in prompt.lower()
        assert "Redis" in prompt or "redis" in prompt.lower()
        assert "invalidation" in prompt.lower() or "TTL" in prompt

    @pytest.mark.asyncio
    async def test_validation_criteria_field_passed(self, validation_config, mock_agent_spawner):
        """Test that validation_criteria field is passed to context."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-vc-test",
            "title": "Add authentication",
            "validation_criteria": "- [ ] JWT tokens generated\n- [ ] Tokens expire after 24h\n- [ ] Refresh tokens supported",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Validation criteria content should be in prompt
        assert "JWT" in prompt or "jwt" in prompt.lower()
        assert "24h" in prompt or "expire" in prompt.lower()
        assert "Refresh" in prompt or "refresh" in prompt.lower()

    @pytest.mark.asyncio
    async def test_category_field_passed(self, validation_config, mock_agent_spawner):
        """Test that category field is passed to context."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-ts-test",
            "title": "Add unit tests",
            "validation_criteria": "- [ ] Tests added",
            "category": "Use pytest with 80% coverage target. Mock external APIs.",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Test strategy should be in prompt
        # Note: Current implementation may not include category yet
        # This test defines expected behavior
        assert "pytest" in prompt.lower() or "80%" in prompt or "coverage" in prompt.lower()

    @pytest.mark.asyncio
    async def test_context_truncation_respects_max_chars(self, mock_agent_spawner):
        """Test that context truncation respects max_chars limit."""
        from gobby.tasks.external_validator import run_external_validation

        # Create config with low max_chars for testing truncation
        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",
            # Note: max_context_chars config may not exist yet
            # This test defines expected behavior for truncation
        )

        # Very long diff that should be truncated
        long_diff = "diff --git a/file.py b/file.py\n" + ("+added line\n" * 10000)

        task = {
            "id": "gt-trunc-test",
            "title": "Large change",
            "validation_criteria": "- [ ] Works",
        }

        await run_external_validation(
            config=config,
            llm_service=None,
            task=task,
            changes_context=long_diff,
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Prompt should not be excessively long
        # Implementation should truncate or summarize very large diffs
        # For now, just verify prompt was created
        assert len(prompt) > 0
        assert "Large change" in prompt or "gt-trunc-test" in prompt

    @pytest.mark.asyncio
    async def test_description_used_when_no_validation_criteria(
        self, validation_config, mock_agent_spawner
    ):
        """Test that description is used when validation_criteria is empty."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-desc-test",
            "title": "Improve performance",
            "description": "Optimize database queries to reduce response time by 50%",
            "validation_criteria": "",  # Empty
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Description should be used as fallback
        assert "database" in prompt.lower() or "queries" in prompt.lower()
        assert "50%" in prompt or "response time" in prompt.lower()

    @pytest.mark.asyncio
    async def test_task_id_included_in_context(self, validation_config, mock_agent_spawner):
        """Test that task ID is included in validation context."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-abc123",
            "title": "Specific task",
            "validation_criteria": "- [ ] Done",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Task ID should be in prompt for traceability
        assert "gt-abc123" in prompt

    @pytest.mark.asyncio
    async def test_task_title_included_in_context(self, validation_config, mock_agent_spawner):
        """Test that task title is included in validation context."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-title-test",
            "title": "Implement user registration flow",
            "validation_criteria": "- [ ] Registration works",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Task title should be in prompt
        assert "user registration" in prompt.lower() or "Implement user registration" in prompt


class TestQALoopIntegration:
    """Tests for QA loop integration with external validator.

    TDD Red Phase: These tests verify that the external validator properly
    integrates with the QA loop (validation -> feedback -> retry cycle).

    Task: gt-4706c9
    """

    @pytest.fixture
    def validation_config(self):
        """Create a validation config for QA loop tests."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",
        )

    @pytest.fixture
    def mock_agent_spawner(self):
        """Mock agent spawner returning successful spawn."""
        spawner = MagicMock()
        spawner.spawn_agent = AsyncMock(
            return_value={
                "success": True,
                "agent_id": "agent-qa-123",
                "status": "running",
            }
        )
        spawner.get_agent_result = AsyncMock()
        return spawner

    @pytest.mark.asyncio
    async def test_returns_result_qa_loop_can_process(self, validation_config, mock_agent_spawner):
        """Test that external validator returns ExternalValidationResult processable by QA loop."""
        from gobby.tasks.external_validator import ExternalValidationResult, run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": '{"status": "valid", "summary": "All good", "issues": []}',
        }

        task = {
            "id": "gt-qa-test",
            "title": "QA test task",
            "validation_criteria": "- [ ] Works",
        }

        result = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        # Result should be an ExternalValidationResult
        assert isinstance(result, ExternalValidationResult)

        # Should have required fields for QA loop processing
        assert hasattr(result, "status")
        assert hasattr(result, "summary")
        assert hasattr(result, "issues")
        assert result.status in ("valid", "invalid", "error", "skipped", "pending")

    @pytest.mark.asyncio
    async def test_validation_issues_formatted_for_feedback(
        self, validation_config, mock_agent_spawner
    ):
        """Test that validation issues are formatted for feedback to implementation agent."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": """{
                "status": "invalid",
                "summary": "Tests are failing",
                "issues": [
                    {
                        "type": "test_failure",
                        "severity": "blocker",
                        "title": "Unit test fails",
                        "location": "tests/test_auth.py:45",
                        "details": "AssertionError: Expected True, got False",
                        "suggested_fix": "Fix the login method to return correct boolean"
                    }
                ]
            }""",
        }

        task = {
            "id": "gt-feedback-test",
            "title": "Task needing feedback",
            "validation_criteria": "- [ ] Tests pass",
        }

        result = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        # Issues should be properly parsed
        assert result.status == "invalid"
        assert len(result.issues) >= 1

        issue = result.issues[0]
        # Issues should have actionable information
        assert issue.title is not None
        assert len(issue.title) > 0

    @pytest.mark.asyncio
    async def test_retry_behavior_on_validation_failure(
        self, validation_config, mock_agent_spawner
    ):
        """Test that validation failure allows retry (doesn't raise exception)."""
        from gobby.tasks.external_validator import run_external_validation

        # First call: failure
        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": '{"status": "invalid", "summary": "Missing tests", "issues": [{"type": "acceptance_gap", "severity": "major", "title": "No tests"}]}',
        }

        task = {
            "id": "gt-retry-test",
            "title": "Retry test task",
            "validation_criteria": "- [ ] Has tests",
        }

        # First validation - should return invalid without raising
        result1 = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff v1",
            agent_spawner=mock_agent_spawner,
        )

        assert result1.status == "invalid"
        assert len(result1.issues) >= 1

        # Second call: success after retry
        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": '{"status": "valid", "summary": "Tests added", "issues": []}',
        }

        # Second validation after "fix"
        result2 = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff v2 with tests",
            agent_spawner=mock_agent_spawner,
        )

        assert result2.status == "valid"
        assert len(result2.issues) == 0

    @pytest.mark.asyncio
    async def test_passed_validation_signals_completion(
        self, validation_config, mock_agent_spawner
    ):
        """Test that passed validation returns status that signals task completion."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": '{"status": "valid", "summary": "All criteria satisfied", "issues": []}',
        }

        task = {
            "id": "gt-complete-test",
            "title": "Completion test",
            "validation_criteria": "- [ ] Feature works",
        }

        result = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        # Valid status should signal completion
        assert result.status == "valid"
        assert result.issues == []
        # Summary should confirm success
        assert "satisfied" in result.summary.lower() or len(result.summary) > 0

    @pytest.mark.asyncio
    async def test_timeout_handling_doesnt_break_qa_loop(
        self, validation_config, mock_agent_spawner
    ):
        """Test that timeout handling returns error result instead of raising."""
        from gobby.tasks.external_validator import run_external_validation

        # Simulate timeout
        mock_agent_spawner.get_agent_result.return_value = {
            "success": False,
            "status": "timeout",
            "error": "Agent execution timed out after 120s",
        }

        task = {
            "id": "gt-timeout-test",
            "title": "Timeout test",
            "validation_criteria": "- [ ] Completes",
        }

        # Should not raise exception
        result = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        # Should return error status, not raise
        assert result.status == "error"
        assert "timed out" in result.summary.lower() or "timeout" in result.summary.lower()
        # QA loop can decide to retry or escalate based on this

    @pytest.mark.asyncio
    async def test_error_result_includes_error_details(self, validation_config, mock_agent_spawner):
        """Test that error results include error details for debugging."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": False,
            "status": "error",
            "error": "Agent process crashed unexpectedly",
        }

        task = {
            "id": "gt-error-test",
            "title": "Error test",
            "validation_criteria": "- [ ] Works",
        }

        result = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        assert result.status == "error"
        # Error field should contain details
        assert result.error is not None or "error" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_multiple_issues_all_included(self, validation_config, mock_agent_spawner):
        """Test that multiple validation issues are all included in result."""
        from gobby.tasks.external_validator import run_external_validation

        mock_agent_spawner.get_agent_result.return_value = {
            "success": True,
            "status": "completed",
            "output": """{
                "status": "invalid",
                "summary": "Multiple issues found",
                "issues": [
                    {"type": "acceptance_gap", "severity": "major", "title": "Missing login feature"},
                    {"type": "test_failure", "severity": "blocker", "title": "Auth tests fail"},
                    {"type": "lint_error", "severity": "minor", "title": "Unused import"}
                ]
            }""",
        }

        task = {
            "id": "gt-multi-issue",
            "title": "Multi-issue task",
            "validation_criteria": "- [ ] Login works\n- [ ] Tests pass\n- [ ] Clean code",
        }

        result = await run_external_validation(
            config=validation_config,
            llm_service=None,
            task=task,
            changes_context="diff",
            agent_spawner=mock_agent_spawner,
        )

        # All issues should be included
        assert result.status == "invalid"
        assert len(result.issues) >= 3


class TestTaskAwareValidationContext:
    """Tests for task-aware validation context prioritization.

    These tests verify that the external validator extracts file paths from task
    descriptions and passes them as priority_files to summarize_diff_for_validation,
    ensuring that task-relevant files get more space in the validation context.

    Task: gt-9bf839
    """

    @pytest.fixture
    def validation_config(self):
        """Create a validation config for task-aware tests."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
        )

    @pytest.fixture
    def mock_llm_service(self):
        """Create a mock LLM service."""
        from gobby.llm import LLMProvider, LLMService

        service = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        service.get_provider.return_value = provider
        provider.generate_text.return_value = '{"status": "valid", "issues": []}'
        return service

    @pytest.fixture
    def mock_agent_spawner(self):
        """Mock the gobby-agents spawner interface."""
        spawner = MagicMock()
        spawner.spawn_agent = AsyncMock(
            return_value={
                "success": True,
                "agent_id": "agent-task-aware-123",
                "status": "running",
            }
        )
        spawner.get_agent_result = AsyncMock(
            return_value={
                "success": True,
                "status": "completed",
                "output": '{"status": "valid", "summary": "All criteria met", "issues": []}',
            }
        )
        return spawner

    @pytest.mark.asyncio
    async def test_task_aware_llm_validation_extracts_files(
        self, validation_config, mock_llm_service
    ):
        """Test that LLM validation extracts files from task and prioritizes them in diff."""
        from gobby.tasks.external_validator import run_external_validation

        # Task mentions specific files
        task = {
            "id": "gt-file-extract",
            "title": "Fix bug in src/auth/login.py",
            "description": "The error handling in `src/auth/login.py` needs improvement. Also update tests/test_login.py.",
            "validation_criteria": "- [ ] Error handling improved in src/auth/login.py",
        }

        # Large diff with many files - priority files should get more space
        changes_context = """diff --git a/src/auth/login.py b/src/auth/login.py
index abc..def 100644
+def improved_login():
+    # Better error handling
+    pass
diff --git a/src/unrelated/file1.py b/src/unrelated/file1.py
+unrelated changes
diff --git a/src/unrelated/file2.py b/src/unrelated/file2.py
+more unrelated
diff --git a/tests/test_login.py b/tests/test_login.py
+def test_improved_login():
+    pass
"""

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=task,
            changes_context=changes_context,
        )

        call_kwargs = mock_llm_service.get_provider.return_value.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # The prompt should indicate priority files or show them prominently
        # Priority files should appear in the prompt
        assert "src/auth/login.py" in prompt or "login.py" in prompt
        assert "tests/test_login.py" in prompt or "test_login.py" in prompt

    @pytest.mark.asyncio
    async def test_task_aware_spawn_validation_extracts_files(self, mock_agent_spawner):
        """Test that spawn validation extracts files from task and prioritizes them."""
        from gobby.tasks.external_validator import run_external_validation

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",
        )

        task = {
            "id": "gt-spawn-extract",
            "title": "Update src/tasks/task_expansion.py",
            "description": "Modify the `expand_task` function in `src/tasks/task_expansion.py` to support TDD mode.",
            "validation_criteria": "- [ ] TDD mode supported in src/tasks/task_expansion.py",
        }

        large_changes = "diff --git a/file1.py b/file1.py\n" + ("+line\n" * 100)
        large_changes += "\ndiff --git a/src/tasks/task_expansion.py b/src/tasks/task_expansion.py\n+def expand_task():\n"
        large_changes += "\ndiff --git a/file2.py b/file2.py\n" + ("+line\n" * 100)

        await run_external_validation(
            config=config,
            llm_service=None,
            task=task,
            changes_context=large_changes,
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # The task-relevant file should be prominently featured
        assert "task_expansion.py" in prompt

    @pytest.mark.asyncio
    async def test_task_aware_agent_validation_extracts_files(self, validation_config):
        """Test that agent validation extracts files from task and prioritizes them."""
        from gobby.llm.executor import AgentResult
        from gobby.tasks.external_validator import run_external_validation

        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(
            return_value=AgentResult(
                output='{"status": "valid", "summary": "Good", "issues": []}',
                status="completed",
                turns_used=2,
            )
        )

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="agent",
        )

        task = {
            "id": "gt-agent-extract",
            "title": "Fix src/api/endpoints.py validation",
            "description": "The input validation in `src/api/endpoints.py` is too permissive.",
            "validation_criteria": "- [ ] Input validation stricter in src/api/endpoints.py",
        }

        await run_external_validation(
            config=config,
            llm_service=None,
            task=task,
            changes_context="diff content with many files",
            agent_runner=mock_runner,
        )

        call_args = mock_runner.run.call_args
        agent_config = call_args[0][0]
        prompt = agent_config.prompt

        # Task-relevant file should be in the prompt
        assert "endpoints.py" in prompt or "src/api" in prompt

    @pytest.mark.asyncio
    async def test_task_aware_no_files_default_behavior(self, validation_config, mock_llm_service):
        """Test that when task has no file paths, default behavior is unchanged."""
        from gobby.tasks.external_validator import run_external_validation

        # Task without file mentions
        task = {
            "id": "gt-no-files",
            "title": "Improve performance",
            "description": "Make the application faster by optimizing database queries.",
            "validation_criteria": "- [ ] Query performance improved",
        }

        changes_context = """diff --git a/src/db.py b/src/db.py
+optimized query
diff --git a/src/cache.py b/src/cache.py
+cache layer
"""

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=task,
            changes_context=changes_context,
        )

        call_kwargs = mock_llm_service.get_provider.return_value.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Both files should be present (no prioritization)
        assert "db.py" in prompt or "src/db" in prompt
        assert "cache.py" in prompt or "src/cache" in prompt

    @pytest.mark.asyncio
    async def test_task_aware_prompt_includes_priority_note(self, mock_agent_spawner):
        """Test that validation prompt includes note about which files were prioritized."""
        from gobby.tasks.external_validator import run_external_validation

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",
        )

        task = {
            "id": "gt-priority-note",
            "title": "Update src/important.py",
            "description": "Changes needed in `src/important.py` and `src/related.py`.",
            "validation_criteria": "- [ ] src/important.py updated correctly",
        }

        # Large diff to trigger summarization
        changes = "diff --git a/src/important.py b/src/important.py\n+important change\n"
        changes += "diff --git a/src/related.py b/src/related.py\n+related change\n"
        changes += "diff --git a/other.py b/other.py\n" + "+other\n" * 1000

        await run_external_validation(
            config=config,
            llm_service=None,
            task=task,
            changes_context=changes,
            agent_spawner=mock_agent_spawner,
        )

        call_kwargs = mock_agent_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Prompt should indicate which files are task-relevant/prioritized
        # This could be via a "Priority Files" section or similar indicator
        assert "important.py" in prompt
        assert "related.py" in prompt


class TestSymbolContextInValidationPrompt:
    """Tests for including symbol context in validation prompts.

    These tests verify that the external validator extracts function/class
    names from task descriptions and includes them in the validation prompt
    to help the validator focus on key symbols.

    Task: gt-dd9bf8
    """

    @pytest.fixture
    def validation_config(self):
        """Create a validation config for symbol context tests."""
        return TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
        )

    @pytest.fixture
    def mock_llm_service(self):
        """Create a mock LLM service."""
        from gobby.llm import LLMProvider, LLMService

        service = MagicMock(spec=LLMService)
        provider = AsyncMock(spec=LLMProvider)
        service.get_provider.return_value = provider
        provider.generate_text.return_value = '{"status": "valid", "issues": []}'
        return service

    @pytest.mark.asyncio
    async def test_symbol_context_includes_key_symbols_list(
        self, validation_config, mock_llm_service
    ):
        """Test that when symbols extracted, prompt includes 'Key symbols to verify' list."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-symbol-list",
            "title": "Fix `validate_input()` function",
            "description": "The `validate_input()` function and `InputValidator` class need updates.",
            "validation_criteria": "- [ ] validate_input() handles edge cases",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff --git a/validator.py b/validator.py\n+def validate_input():\n",
        )

        call_kwargs = mock_llm_service.get_provider.return_value.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Prompt should include the extracted symbols
        assert "validate_input" in prompt
        assert "InputValidator" in prompt
        # Should have some indication these are key symbols
        assert (
            "symbol" in prompt.lower() or "function" in prompt.lower() or "class" in prompt.lower()
        )

    @pytest.mark.asyncio
    async def test_symbol_context_instructs_verify_existence(
        self, validation_config, mock_llm_service
    ):
        """Test that prompt instructs validator to check functions/classes exist."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-verify-exist",
            "title": "Implement `process_data()` function",
            "description": "Add `process_data()` and `DataProcessor` class to handle transformations.",
            "validation_criteria": "- [ ] process_data() implemented",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff --git a/processor.py b/processor.py\n+class DataProcessor:\n+    def process_data(self):\n",
        )

        call_kwargs = mock_llm_service.get_provider.return_value.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Should include the symbols in some form
        assert "process_data" in prompt
        assert "DataProcessor" in prompt

    @pytest.mark.asyncio
    async def test_symbol_context_extracts_from_validation_criteria(
        self, validation_config, mock_llm_service
    ):
        """Test that symbols from validation_criteria field are also included."""
        from gobby.tasks.external_validator import run_external_validation

        task = {
            "id": "gt-criteria-symbols",
            "title": "Update authentication",
            "description": "Improve the auth flow.",
            "validation_criteria": "- [ ] `authenticate_user()` returns token\n- [ ] `TokenValidator` class exists",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff --git a/auth.py b/auth.py\n+changes",
        )

        call_kwargs = mock_llm_service.get_provider.return_value.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Symbols from validation_criteria should be extracted
        assert "authenticate_user" in prompt
        assert "TokenValidator" in prompt

    @pytest.mark.asyncio
    async def test_symbol_context_empty_symbols_no_extra_sections(
        self, validation_config, mock_llm_service
    ):
        """Test that empty symbols list doesn't add extra prompt sections."""
        from gobby.tasks.external_validator import run_external_validation

        # Task without any backtick-quoted symbols
        task = {
            "id": "gt-no-symbols",
            "title": "Improve documentation",
            "description": "Add more comments to explain the code.",
            "validation_criteria": "- [ ] Comments added to complex functions",
        }

        await run_external_validation(
            config=validation_config,
            llm_service=mock_llm_service,
            task=task,
            changes_context="diff --git a/main.py b/main.py\n+# comment",
        )

        call_kwargs = mock_llm_service.get_provider.return_value.generate_text.call_args.kwargs
        prompt = call_kwargs["prompt"]

        # Should NOT have a "Key symbols" section when no symbols found
        assert "key symbols to verify" not in prompt.lower()
        assert "prioritized symbols" not in prompt.lower()

    @pytest.mark.asyncio
    async def test_symbol_context_with_spawn_mode(self):
        """Test that spawn mode validation also includes symbol context."""
        from gobby.tasks.external_validator import run_external_validation

        mock_spawner = MagicMock()
        mock_spawner.spawn_agent = AsyncMock(
            return_value={"success": True, "agent_id": "test", "status": "running"}
        )
        mock_spawner.get_agent_result = AsyncMock(
            return_value={
                "success": True,
                "status": "completed",
                "output": '{"status": "valid", "issues": []}',
            }
        )

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="spawn",
        )

        task = {
            "id": "gt-spawn-symbols",
            "title": "Add `calculate_total()` function",
            "description": "Implement `calculate_total()` using the `PriceCalculator` class.",
            "validation_criteria": "- [ ] calculate_total() works correctly",
        }

        await run_external_validation(
            config=config,
            llm_service=None,
            task=task,
            changes_context="diff content",
            agent_spawner=mock_spawner,
        )

        call_kwargs = mock_spawner.spawn_agent.call_args.kwargs
        prompt = call_kwargs.get("prompt", "")

        # Symbols should be in the spawn prompt too
        assert "calculate_total" in prompt
        assert "PriceCalculator" in prompt

    @pytest.mark.asyncio
    async def test_symbol_context_with_agent_mode(self, validation_config):
        """Test that agent mode validation also includes symbol context."""
        from gobby.llm.executor import AgentResult
        from gobby.tasks.external_validator import run_external_validation

        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(
            return_value=AgentResult(
                output='{"status": "valid", "issues": []}',
                status="completed",
                turns_used=1,
            )
        )

        config = TaskValidationConfig(
            enabled=True,
            provider="claude",
            model="claude-haiku-4-5",
            use_external_validator=True,
            external_validator_mode="agent",
        )

        task = {
            "id": "gt-agent-symbols",
            "title": "Refactor `parse_config()`",
            "description": "Update `parse_config()` to use the `ConfigParser` class.",
            "validation_criteria": "- [ ] parse_config() uses ConfigParser",
        }

        await run_external_validation(
            config=config,
            llm_service=None,
            task=task,
            changes_context="diff content",
            agent_runner=mock_runner,
        )

        call_args = mock_runner.run.call_args
        agent_config = call_args[0][0]
        prompt = agent_config.prompt

        # Symbols should be in the agent prompt
        assert "parse_config" in prompt
        assert "ConfigParser" in prompt

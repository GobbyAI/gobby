from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.actions import ActionContext, ActionExecutor
from gobby.workflows.definitions import WorkflowState
from gobby.workflows.templates import TemplateEngine
from gobby.memory.manager import MemoryManager
from gobby.memory.skills import SkillLearner


@pytest.fixture
def mock_mem_services():
    services = {
        "template_engine": MagicMock(spec=TemplateEngine),
        "llm_service": AsyncMock(),
        "transcript_processor": MagicMock(),
        "config": MagicMock(),
        "mcp_manager": AsyncMock(),
        "memory_manager": MagicMock(spec=MemoryManager),
        "skill_learner": AsyncMock(spec=SkillLearner),
    }
    # Manually attach config mocks because spec might strict on attributes not in class __init__
    services["memory_manager"].config = MagicMock()
    services["memory_manager"].config.enabled = True
    services["skill_learner"].config = MagicMock()
    services["skill_learner"].config.enabled = True
    return services


@pytest.fixture
def mem_action_executor(temp_db, session_manager, mock_mem_services):
    executor = ActionExecutor(
        temp_db,
        session_manager,
        mock_mem_services["template_engine"],
        llm_service=mock_mem_services["llm_service"],
        transcript_processor=mock_mem_services["transcript_processor"],
        config=mock_mem_services["config"],
        mcp_manager=mock_mem_services["mcp_manager"],
        memory_manager=mock_mem_services["memory_manager"],
        skill_learner=mock_mem_services["skill_learner"],
    )
    # Ensure handlers are registered
    return executor


@pytest.fixture
def mem_workflow_state():
    return WorkflowState(
        session_id="test-session-id", workflow_name="test-workflow", phase="test-phase"
    )


@pytest.fixture
def mem_action_context(temp_db, session_manager, mem_workflow_state, mock_mem_services):
    return ActionContext(
        session_id=mem_workflow_state.session_id,
        state=mem_workflow_state,
        db=temp_db,
        session_manager=session_manager,
        template_engine=MagicMock(spec=TemplateEngine),
        mcp_manager=mock_mem_services["mcp_manager"],
        memory_manager=mock_mem_services["memory_manager"],
        skill_learner=mock_mem_services["skill_learner"],
    )


@pytest.mark.asyncio
async def test_memory_inject_recall(
    mem_action_executor, mem_action_context, session_manager, sample_project, mock_mem_services
):
    # Setup session
    session = session_manager.register(
        external_id="mem-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
    )
    mem_action_context.session_id = session.id
    mem_action_context.state.session_id = session.id

    # Mock recall
    mock_mem_services["memory_manager"].config.enabled = True

    m1 = MagicMock()
    m1.memory_type = "fact"
    m1.content = "Memory 1"
    m2 = MagicMock()
    m2.memory_type = "learning"
    m2.content = "Memory 2"

    mock_mem_services["memory_manager"].recall.return_value = [m1, m2]

    # Execute
    result = await mem_action_executor.execute(
        "memory_inject", mem_action_context, min_importance=0.7
    )

    # Verify
    assert result is not None
    assert "inject_context" in result
    assert "Memory 1" in result["inject_context"]

    mock_mem_services["memory_manager"].recall.assert_called_with(
        project_id=str(sample_project["id"]), min_importance=0.7
    )


@pytest.mark.asyncio
async def test_memory_inject_skills(
    mem_action_executor, mem_action_context, session_manager, sample_project, mock_mem_services
):
    session = session_manager.register(
        external_id="skill-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
    )
    mem_action_context.session_id = session.id

    # Mock
    mock_mem_services["memory_manager"].config.enabled = True
    mock_mem_services["memory_manager"].recall.return_value = []

    # Mock Skill Match
    mock_mem_services["skill_learner"].config.enabled = True
    mock_skill = MagicMock()
    mock_skill.name = "Test Skill"
    mock_skill.content = "Content"
    mock_mem_services["skill_learner"].match_skills.return_value = [mock_skill]

    result = await mem_action_executor.execute(
        "memory_inject", mem_action_context, prompt="How do I...?"
    )

    assert result is not None
    assert "inject_context" in result
    assert "Test Skill" in result["inject_context"]

    mock_mem_services["skill_learner"].match_skills.assert_called_with(
        "How do I...?", str(sample_project["id"])
    )


@pytest.mark.asyncio
async def test_skills_learn(
    mem_action_executor, mem_action_context, session_manager, sample_project, mock_mem_services
):
    session = session_manager.register(
        external_id="learn-ext",
        machine_id="test-machine",
        source="test-source",
        project_id=sample_project["id"],
    )
    mem_action_context.session_id = session.id

    mock_mem_services["skill_learner"].config.enabled = True
    mock_mem_services["skill_learner"].learn_from_session.return_value = [
        MagicMock(name="NewSkill")
    ]

    result = await mem_action_executor.execute("skills_learn", mem_action_context)

    assert result is not None
    assert result["skills_learned"] == 1
    mock_mem_services["skill_learner"].learn_from_session.assert_called_once()

"""Session-aware skill discovery, independent of automatic context injection."""

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.selectors import resolve_excluded_skills_for_agent
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.variable_defaults import resolve_session_project_id


def get_session_skill_exclusions(
    db: HubDatabase, session_id: str, project_id: str | None
) -> set[str]:
    """Resolve explicit exclusions against the current installed skill catalog.

    Include selectors control automatic injection. Only exclude selectors hide
    skills from discovery. Read the current persona so switching personas and
    installing skills cannot leave a stale discovery allowlist behind.
    """
    variables = SessionVariableManager(db).get_variables(session_id)
    agent_name = variables.get("_persona_name") or variables.get("_agent_type")
    if not isinstance(agent_name, str) or not agent_name or agent_name == "none":
        return set()
    if project_id is None:
        project_id = resolve_session_project_id(db, session_id)
    agent = resolve_agent(agent_name, db, project_id=project_id)
    if agent is None:
        return set()
    selectors = agent.workflows.skill_selectors
    if selectors is None or not selectors.exclude:
        return set()

    storage = LocalSkillManager(db)
    excluded: set[str] = set()
    offset = 0
    page_size = 100
    while True:
        batch = storage.list_skills(
            project_id=project_id, include_global=True, limit=page_size, offset=offset
        )
        excluded.update(resolve_excluded_skills_for_agent(agent, batch))
        if len(batch) < page_size:
            return excluded
        offset += page_size

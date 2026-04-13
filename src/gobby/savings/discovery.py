"""Discovery token savings calculation."""

from __future__ import annotations

import logging

from gobby.savings.tracker import SavingsTracker
from gobby.storage.database import DatabaseProtocol
from gobby.workflows.state_manager import SessionVariableManager

logger = logging.getLogger(__name__)


def record_discovery_savings(
    db: DatabaseProtocol, session_id: str, project_id: str, model: str | None
) -> None:
    """Calculate and record progressive discovery savings for a session.

    Compares the total token footprint of all available skills and tools
    against the subset actually loaded during the session.
    """
    try:
        # 1. Skills Calculation
        skills_rows = db.fetchall(
            "SELECT name, content FROM skills WHERE enabled = 1 AND source != 'template'"
        )
        all_skills_chars = sum(len(row["content"] or "") for row in skills_rows)

        session_skills_rows = db.fetchall(
            "SELECT skill_name FROM session_skills WHERE session_id = ?",
            (session_id,),
        )
        used_skill_names = {row["skill_name"] for row in session_skills_rows}

        used_skills_chars = sum(
            len(row["content"] or "") for row in skills_rows if row["name"] in used_skill_names
        )

        # 2. Tools Calculation
        tools_rows = db.fetchall(
            """
            SELECT t.name as tool_name, s.name as server_name, t.input_schema
            FROM tools t
            JOIN mcp_servers s ON t.mcp_server_id = s.id
            WHERE s.project_id = ?
            """,
            (project_id,),
        )

        all_tools_chars = sum(len(row["input_schema"] or "") for row in tools_rows)

        var_mgr = SessionVariableManager(db)
        vars_dict = var_mgr.get_variables(session_id)
        unlocked_tools = vars_dict.get("unlocked_tools", [])

        used_tools_chars = 0
        used_tools_count = 0
        for row in tools_rows:
            key = f"{row['server_name']}:{row['tool_name']}"
            if key in unlocked_tools:
                used_tools_chars += len(row["input_schema"] or "")
                used_tools_count += 1

        # 3. Record Savings
        original_chars = all_skills_chars + all_tools_chars
        actual_chars = used_skills_chars + used_tools_chars

        if original_chars > 0:
            tracker = SavingsTracker(db=db)
            tracker.record(
                category="discovery",
                original_chars=original_chars,
                actual_chars=actual_chars,
                session_id=session_id,
                project_id=project_id,
                model=model,
                metadata={
                    "all_skills_count": len(skills_rows),
                    "used_skills_count": len(used_skill_names),
                    "all_tools_count": len(tools_rows),
                    "used_tools_count": used_tools_count,
                },
            )
            logger.debug(
                f"Recorded discovery savings for session {session_id}: original_chars={original_chars}, actual_chars={actual_chars}",
            )
    except Exception as e:
        logger.warning(f"Failed to calculate discovery savings for session {session_id}: {e}")

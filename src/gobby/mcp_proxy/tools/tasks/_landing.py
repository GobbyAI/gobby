"""Landing operation tools for gobby-tasks-ops."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import (
    CHECKOUT_RESOLUTION_ERRORS,
    RegistryContext,
    checkout_unresolved_error,
)
from gobby.tasks.landing_policy import freeze_path, write_freeze
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.utils.session_context import get_current_session_id


def create_landing_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create landing operation tools for gobby-tasks-ops."""
    registry = InternalToolRegistry(
        name="gobby-tasks-landing",
        description="Landing operation tools",
    )

    async def set_landing_freeze(on: bool, reason: str) -> dict[str, Any]:
        """Set or clear the landing freeze for the calling session's project."""
        session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "set_landing_freeze requires a calling session"}
        reason = reason.strip()
        if on and not reason:
            return {"success": False, "error": "Setting the landing freeze requires a reason"}
        try:
            project_id = ctx.resolve_project_from_session(session_id)
            repo_path = ctx.get_project_repo_path(
                project_id, ctx.checkout_machine_id(project_id, session_id)
            )
        except CHECKOUT_RESOLUTION_ERRORS as exc:
            return checkout_unresolved_error(exc)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        if not repo_path:
            return {"success": False, "error": f"No checkout for project {project_id}"}

        result = await daemon_git.run(
            ("rev-parse", "--path-format=absolute", "--git-common-dir"), cwd=repo_path
        )
        if not isinstance(result, GitOk) or not result.stdout.strip():
            detail = result.stderr.strip() or result.status
            return {
                "success": False,
                "error": f"Cannot resolve the git common dir of {repo_path}: {detail}",
            }
        common_dir = result.stdout.strip()
        try:
            freeze = await asyncio.to_thread(
                write_freeze, common_dir, on=on, reason=reason, session_id=session_id
            )
        except OSError as exc:
            return {"success": False, "error": f"Cannot write the landing freeze: {exc}"}
        return {"success": True, "freeze": asdict(freeze), "path": str(freeze_path(common_dir))}

    registry.register(
        name="set_landing_freeze",
        description=(
            "Set or clear the landing freeze for the calling session's project. Setting it "
            "(on=true) requires a non-empty reason. The state, the setting or clearing "
            "session and the time are stored at <git-common-dir>/gobby/landing-freeze.json."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true sets the freeze, false clears it"},
                "reason": {"type": "string", "description": "Why the freeze is set or cleared"},
            },
            "required": ["on", "reason"],
        },
        func=set_landing_freeze,
    )

    return registry

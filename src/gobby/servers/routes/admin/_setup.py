"""Setup endpoints for admin router (Web onboarding)."""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
from fastapi import APIRouter
from pydantic import BaseModel

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class SetupStateUpdate(BaseModel):
    web_onboarding_complete: bool = False


def register_setup_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/setup-state")
    async def get_setup_state() -> dict[str, Any]:
        """Return the setup wizard state from ``~/.gobby/setup_state.json``."""
        state_path = Path("~/.gobby/setup_state.json").expanduser()
        if not state_path.exists():
            return {"exists": False}
        try:
            async with aiofiles.open(state_path) as f:
                content = await f.read()
            data: dict[str, Any] = json.loads(content)
            data["exists"] = True
            return data
        except (json.JSONDecodeError, OSError) as exc:
            return {"exists": False, "error": str(exc)}

    @router.post("/setup-state")
    async def update_setup_state(request: SetupStateUpdate) -> dict[str, Any]:
        """Allow the web UI to mark web onboarding as complete."""
        state_path = Path("~/.gobby/setup_state.json").expanduser()
        if not state_path.exists():
            return {"success": False, "error": "No setup state found"}
        try:
            async with aiofiles.open(state_path) as f:
                content = await f.read()
            data = json.loads(content)
            if request.web_onboarding_complete:
                data["web_onboarding_complete"] = True

            async with aiofiles.open(state_path, mode="w") as f:
                await f.write(json.dumps(data, indent=2))
            return {"success": True}
        except (json.JSONDecodeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

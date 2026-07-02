from __future__ import annotations

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.artifacts import create_artifacts_registry, set_artifact_broadcaster
from gobby.utils.project_context import reset_project_context, set_project_context

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_show_file_rejects_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = project / "escape.txt"
    link.symlink_to(outside)
    token = set_project_context(
        {"id": "11111111-1111-4111-8111-111111110001", "project_path": str(project)}
    )
    try:
        registry = create_artifacts_registry()
        result = await registry.call(
            "show_file",
            {"file_path": str(link), "conversation_id": "conv-1"},
        )
    finally:
        reset_project_context(token)

    assert result["success"] is False
    assert "allowed project roots" in result["error"]


@pytest.mark.asyncio
async def test_show_file_allows_explicit_artifact_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    note = allowed / "note.txt"
    note.write_text("visible\n", encoding="utf-8")
    token = set_project_context(
        {
            "id": "11111111-1111-4111-8111-111111110001",
            "project_path": str(project),
            "artifact_allowed_roots": [str(allowed)],
        }
    )
    set_artifact_broadcaster(None)
    try:
        registry = create_artifacts_registry()
        result = await registry.call(
            "show_file",
            {"file_path": str(note), "conversation_id": "conv-1"},
        )
    finally:
        reset_project_context(token)

    assert result == {
        "success": True,
        "type": "text",
        "language": "plaintext",
        "title": "note.txt",
    }

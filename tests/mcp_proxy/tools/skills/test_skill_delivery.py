"""Contract tests for brief skill projections and lossless delivery paging."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillFile

pytestmark = pytest.mark.integration

RESPONSE_BUDGET_BYTES = 15_000


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    yield temp_db


@pytest.fixture
def storage(db: HubDatabase) -> LocalSkillManager:
    return LocalSkillManager(db)


def _skill_file(skill_id: str, path: str, content: str) -> SkillFile:
    encoded = content.encode("utf-8")
    return SkillFile(
        id="",
        skill_id=skill_id,
        path=path,
        file_type="reference",
        content=content,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
    )


def _serialized_size(response: dict[str, Any]) -> int:
    return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _required_tool(registry: Any, name: str) -> Any:
    tool = registry.get_tool(name)
    assert tool is not None
    return tool


async def _collect_skill_pages(tool: Any, **arguments: Any) -> tuple[list[dict[str, Any]], str]:
    pages: list[dict[str, Any]] = []
    response = await tool(**arguments)
    while True:
        pages.append(response)
        cursor = response["page"]["next_cursor"]
        if cursor is None:
            break
        response = await tool(cursor=cursor)
    return pages, "".join(page["skill"]["content"] for page in pages)


def _collect_file_pages(tool: Any, **arguments: Any) -> tuple[list[dict[str, Any]], str]:
    pages: list[dict[str, Any]] = []
    response = tool(**arguments)
    while True:
        pages.append(response)
        cursor = response["page"]["next_cursor"]
        if cursor is None:
            break
        response = tool(cursor=cursor)
    return pages, "".join(page["file"]["content"] for page in pages)


@pytest.mark.asyncio
async def test_get_skill_defaults_to_brief_projection(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    skill = storage.create_skill(
        name="brief-projection",
        description="Management-only description",
        content="# Brief\n\nInstructions",
        version="1.2.3",
        license="MIT",
        compatibility="Codex",
        allowed_tools=["Read"],
        metadata={"private": "verbose"},
        source_type="filesystem",
        source_path="/bundled/brief-projection",
    )
    storage.set_skill_files(
        skill.id,
        [_skill_file(skill.id, "references/topic.md", "# Topic\n\nDetails")],
    )

    tool = _required_tool(create_skills_registry(db), "get_skill")
    response = await tool(name=skill.name)

    assert response == {
        "success": True,
        "view": "brief",
        "skill": {
            "name": "brief-projection",
            "content": "# Brief\n\nInstructions",
            "compatibility": "Codex",
            "allowed_tools": ["Read"],
        },
        "page": {"complete": True, "next_cursor": None},
        "references": {
            "entries": [{"path": "references/topic.md", "size_bytes": len("# Topic\n\nDetails")}],
            "remaining_count": 0,
            "next_after_path": None,
        },
    }


@pytest.mark.asyncio
async def test_get_skill_explicit_full_projection_retains_management_metadata(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    skill = storage.create_skill(
        name="full-projection",
        description="Full description",
        content="# Full",
        version="2.0.0",
        license="Apache-2.0",
        compatibility="All agents",
        allowed_tools=["Read"],
        metadata={"owner": "gobby"},
        source_type="filesystem",
        source_path="/bundled/full-projection",
        source_ref="0.5.0",
    )

    tool = _required_tool(create_skills_registry(db), "get_skill")
    response = await tool(name=skill.name, brief=False)

    assert response["success"] is True
    assert response["view"] == "full"
    assert response["skill"] == {
        "id": skill.id,
        "name": "full-projection",
        "description": "Full description",
        "content": "# Full",
        "version": "2.0.0",
        "license": "Apache-2.0",
        "compatibility": "All agents",
        "allowed_tools": ["Read"],
        "metadata": {"owner": "gobby"},
        "enabled": True,
        "source": {
            "scope": "installed",
            "type": "filesystem",
            "path": "/bundled/full-projection",
            "ref": "0.5.0",
        },
    }
    assert response["page"]["start_byte"] == 0
    assert response["page"]["end_byte"] == len("# Full")
    assert response["page"]["total_bytes"] == len("# Full")
    assert response["page"]["complete"] is True
    assert response["page"]["next_cursor"] is None
    assert len(response["page"]["content_hash"]) == 64
    assert response["files"]["entries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        ("A" * 401 + "\n\n") * 60,
        ("🙂界" * 140 + "\n\n") * 60,
    ],
    ids=["ascii", "multibyte"],
)
async def test_get_skill_pages_reassemble_exact_content_at_semantic_boundaries(
    db: HubDatabase, storage: LocalSkillManager, content: str
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    skill = storage.create_skill(name="paged-skill", description="Paged", content=content)
    tool = _required_tool(create_skills_registry(db), "get_skill")

    pages, reconstructed = await _collect_skill_pages(tool, name=skill.name)

    assert len(pages) > 1
    assert reconstructed.encode("utf-8") == content.encode("utf-8")
    assert all(_serialized_size(page) <= RESPONSE_BUDGET_BYTES for page in pages)
    assert all(page["skill"]["content"].endswith("\n\n") for page in pages[:-1])
    assert pages[-1]["page"] == {"complete": True, "next_cursor": None}


@pytest.mark.asyncio
async def test_cursor_continuation_preserves_full_view(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    content = ("paragraph\n\n" * 2_000) + "tail"
    skill = storage.create_skill(name="full-pages", description="Paged", content=content)
    tool = _required_tool(create_skills_registry(db), "get_skill")

    first = await tool(name=skill.name, brief=False)
    second = await tool(cursor=first["page"]["next_cursor"])

    assert first["view"] == "full"
    assert second["view"] == "full"
    assert second["page"]["start_byte"] == first["page"]["end_byte"]


@pytest.mark.asyncio
async def test_get_skill_file_pages_reassemble_exact_content(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    content = ("Reference paragraph 🙂\n\n" * 1_200) + "tail"
    skill = storage.create_skill(name="file-pages", description="Files", content="# Router")
    storage.set_skill_files(skill.id, [_skill_file(skill.id, "references/topic.md", content)])
    tool = _required_tool(create_skills_registry(db), "get_skill_file")

    pages, reconstructed = _collect_file_pages(tool, name=skill.name, path="references/topic.md")

    assert len(pages) > 1
    assert reconstructed.encode("utf-8") == content.encode("utf-8")
    assert all(page["view"] == "brief" for page in pages)
    assert all(_serialized_size(page) <= RESPONSE_BUDGET_BYTES for page in pages)
    assert pages[0]["file"]["skill_name"] == skill.name
    assert pages[0]["file"]["path"] == "references/topic.md"


@pytest.mark.asyncio
async def test_cursor_errors_are_structured_and_tool_bound(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    content = "entry\n\n" * 4_000
    skill = storage.create_skill(name="cursor-errors", description="Cursor", content=content)
    registry = create_skills_registry(db)
    skill_tool = _required_tool(registry, "get_skill")
    file_tool = _required_tool(registry, "get_skill_file")
    first = await skill_tool(name=skill.name)
    cursor = first["page"]["next_cursor"]

    malformed = await skill_tool(cursor="not-a-cursor")
    mixed = await skill_tool(name=skill.name, cursor=cursor)
    wrong_tool = file_tool(cursor=cursor)

    for response in (malformed, mixed, wrong_tool):
        assert response["success"] is False
        assert response["error_code"] == "invalid_cursor"
        assert set(response).issubset({"success", "error_code", "message", "restart"})


@pytest.mark.asyncio
async def test_content_change_invalidates_cursor(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    skill = storage.create_skill(
        name="stale-pages", description="Stale", content="original\n\n" * 3_000
    )
    tool = _required_tool(create_skills_registry(db), "get_skill")
    first = await tool(name=skill.name)
    storage.update_skill(skill.id, content="replacement\n\n" * 3_000)

    response = await tool(cursor=first["page"]["next_cursor"])

    assert response["success"] is False
    assert response["error_code"] == "stale_cursor"
    assert "restart" in response


@pytest.mark.asyncio
async def test_skill_tracking_occurs_only_after_final_entrypoint_page(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry
    from gobby.workflows.state_manager import SessionVariableManager

    project = LocalProjectManager(db).create(name="paging", repo_path="/tmp/paging")
    session = SessionManager(db).register(
        external_id="paging-session",
        machine_id=None,
        source="codex",
        project_id=project.id,
    )
    skill = storage.create_skill(
        name="tracked-pages",
        description="Tracked",
        content="tracked content\n\n" * 3_000,
        metadata={"gobby": {"levels": ["normal", "max"], "default_level": "normal"}},
    )
    tool = _required_tool(create_skills_registry(db), "get_skill")

    response = await tool(name=skill.name, level="max", session_id=session.id)

    assert response["page"]["complete"] is False
    assert (
        db.fetchone("SELECT skill_name FROM session_skills WHERE session_id = %s", (session.id,))
        is None
    )
    assert SessionVariableManager(db).get_variables(session.id) == {}

    while response["page"]["next_cursor"] is not None:
        response = await tool(cursor=response["page"]["next_cursor"], session_id=session.id)

    row = db.fetchone("SELECT skill_name FROM session_skills WHERE session_id = %s", (session.id,))
    variables = SessionVariableManager(db).get_variables(session.id)
    assert row is not None and row["skill_name"] == skill.name
    assert variables["loaded_skills"] == [skill.name]
    assert variables["tracked_pages_level"] == "max"


@pytest.mark.asyncio
async def test_complete_content_is_scanned_before_any_page_is_served(
    db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    content = ("safe prefix\n\n" * 2_000) + "MALICIOUS_TAIL"
    skill = storage.create_skill(
        name="scan-tail",
        description="External",
        content=content,
        source_type="github",
        source_path="owner/repo",
    )
    scanned: list[str] = []

    def reject_tail(value: str, **_kwargs: Any) -> dict[str, Any]:
        scanned.append(value)
        return {
            "is_safe": "MALICIOUS_TAIL" not in value,
            "max_severity": "high",
            "findings": [],
        }

    tool = _required_tool(create_skills_registry(db), "get_skill")
    with patch("gobby.skills.scanner.scan_served_content", side_effect=reject_tail):
        response = await tool(name=skill.name)

    assert response["success"] is False
    assert scanned == [content]
    assert "skill" not in response

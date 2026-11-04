"""Contract tests for one-call batch delivery of several instruction bodies."""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.skills import LocalSkillManager, SkillFile
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration

RESPONSE_BUDGET_BYTES = 15_000


@pytest.fixture
def storage(temp_db: HubDatabase) -> LocalSkillManager:
    return LocalSkillManager(temp_db)


@pytest.fixture
def session(isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase) -> Session:
    project = isolated_checkout_factory(temp_db, "batch").project
    return SessionManager(temp_db).register(
        external_id="batch-session",
        machine_id=None,
        source="claude",
        project_id=project.id,
    )


def _reference(skill_id: str, path: str, content: str) -> SkillFile:
    encoded = content.encode("utf-8")
    return SkillFile(
        id="",
        skill_id=skill_id,
        path=path,
        file_type="reference",
        content=content,
        content_hash="",
        size_bytes=len(encoded),
    )


def _get_skill(temp_db: HubDatabase) -> Any:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    tool = create_skills_registry(temp_db).get_tool("get_skill")
    assert tool is not None
    return tool


def _marked(identity: str, body: str) -> str:
    return f"{body}<!-- end: {identity} -->\n"


def _size(response: dict[str, Any]) -> int:
    return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _ledgers(temp_db: HubDatabase, session_id: str) -> tuple[list[str], list[str]]:
    from gobby.workflows.state_manager import SessionVariableManager

    variables = SessionVariableManager(temp_db).get_variables(session_id)
    return variables.get("loaded_skills", []), variables.get("loaded_skill_references", [])


@pytest.mark.asyncio
async def test_small_bodies_pack_into_one_page_in_request_order(
    temp_db: HubDatabase, storage: LocalSkillManager, session: Session
) -> None:
    alpha = storage.create_skill(name="alpha", description="a", content="# Alpha\n\nA body\n")
    storage.set_skill_files(alpha.id, [_reference(alpha.id, "references/x.md", "# X\n\nref\n")])
    storage.create_skill(name="beta", description="b", content="# Beta\n\nB body\n")

    response = await _get_skill(temp_db)(
        names=["beta", "alpha:references/x.md", "alpha"], session_id=session.id
    )

    assert response == {
        "success": True,
        "view": "brief",
        "instructions": [
            {"identity": "beta", "content": _marked("beta", "# Beta\n\nB body\n")},
            {
                "identity": "alpha:references/x.md",
                "content": _marked("alpha:references/x.md", "# X\n\nref\n"),
            },
            {"identity": "alpha", "content": _marked("alpha", "# Alpha\n\nA body\n")},
        ],
        "page": {
            "completed": ["beta", "alpha:references/x.md", "alpha"],
            "complete": True,
            "next_cursor": None,
        },
    }
    assert _ledgers(temp_db, session.id) == (["beta", "alpha"], ["alpha:references/x.md"])


@pytest.mark.asyncio
async def test_body_that_does_not_fit_moves_whole_to_the_next_page(
    temp_db: HubDatabase, storage: LocalSkillManager, session: Session
) -> None:
    first = "first line\n" * 800
    second = "second line\n" * 800
    storage.create_skill(name="first", description="f", content=first)
    storage.create_skill(name="second", description="s", content=second)
    tool = _get_skill(temp_db)

    page_one = await tool(names=["first", "second"], session_id=session.id)

    assert [item["identity"] for item in page_one["instructions"]] == ["first"]
    assert page_one["instructions"][0]["content"] == _marked("first", first)
    assert page_one["page"]["completed"] == ["first"]
    assert page_one["page"]["complete"] is False
    assert _size(page_one) <= RESPONSE_BUDGET_BYTES
    assert _ledgers(temp_db, session.id) == (["first"], [])

    page_two = await tool(cursor=page_one["page"]["next_cursor"], session_id=session.id)

    assert page_two["instructions"] == [{"identity": "second", "content": _marked("second", second)}]
    assert page_two["page"] == {"completed": ["second"], "complete": True, "next_cursor": None}
    assert _ledgers(temp_db, session.id) == (["first", "second"], [])


@pytest.mark.asyncio
async def test_oversized_body_pages_alone_and_credits_only_on_its_final_page(
    temp_db: HubDatabase, storage: LocalSkillManager, session: Session
) -> None:
    small = "small body\n"
    large = "large paragraph line\n\n" * 2_000
    storage.create_skill(name="small", description="s", content=small)
    storage.create_skill(name="large", description="l", content=large)
    tool = _get_skill(temp_db)

    response = await tool(names=["small", "large"], session_id=session.id)
    assert response["instructions"] == [{"identity": "small", "content": _marked("small", small)}]
    assert response["page"]["completed"] == ["small"]

    chunks: list[str] = []
    pages = 0
    while response["page"]["next_cursor"] is not None:
        response = await tool(cursor=response["page"]["next_cursor"], session_id=session.id)
        pages += 1
        assert _size(response) <= RESPONSE_BUDGET_BYTES
        assert [item["identity"] for item in response["instructions"]] == ["large"]
        chunks.append(response["instructions"][0]["content"])
        if response["page"]["next_cursor"] is not None:
            assert response["page"]["completed"] == []
            assert _ledgers(temp_db, session.id) == (["small"], [])

    assert pages > 1
    assert "".join(chunks) == _marked("large", large)
    assert response["page"] == {"completed": ["large"], "complete": True, "next_cursor": None}
    assert _ledgers(temp_db, session.id) == (["small", "large"], [])


@pytest.mark.asyncio
async def test_levels_map_selects_each_leveled_skill_level(
    temp_db: HubDatabase, storage: LocalSkillManager, session: Session
) -> None:
    from gobby.workflows.state_manager import SessionVariableManager

    storage.create_skill(
        name="leveled-skill",
        description="l",
        content="leveled body\n",
        metadata={"gobby": {"levels": ["lite", "max"], "default_level": "max"}},
    )

    response = await _get_skill(temp_db)(
        names=["leveled-skill"], levels={"leveled-skill": "lite"}, session_id=session.id
    )

    assert response["instructions"][0]["content"] == _marked(
        "leveled-skill", "Active level: lite\n\nleveled body\n"
    )
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert variables["leveled_skill_level"] == "lite"


@pytest.mark.asyncio
async def test_invalid_level_for_a_batch_name_is_refused(
    temp_db: HubDatabase, storage: LocalSkillManager
) -> None:
    storage.create_skill(
        name="leveled-skill",
        description="l",
        content="leveled body\n",
        metadata={"gobby": {"levels": ["lite", "max"], "default_level": "max"}},
    )

    response = await _get_skill(temp_db)(names=["leveled-skill"], levels={"leveled-skill": "huge"})

    assert response["success"] is False
    assert response["error_code"] == "invalid_level"


@pytest.mark.asyncio
async def test_unavailable_identities_are_reported_and_the_rest_are_served(
    temp_db: HubDatabase, storage: LocalSkillManager, session: Session
) -> None:
    storage.create_skill(name="present", description="p", content="present body\n")

    response = await _get_skill(temp_db)(
        names=["present", "absent", "present:references/missing.md", "bad:identity"],
        session_id=session.id,
    )

    assert response["page"]["completed"] == ["present"]
    assert [entry["identity"] for entry in response["unavailable"]] == [
        "absent",
        "present:references/missing.md",
        "bad:identity",
    ]
    assert [entry["error_code"] for entry in response["unavailable"]] == [
        "not_found",
        "not_found",
        "invalid_identity",
    ]
    assert _ledgers(temp_db, session.id) == (["present"], [])


@pytest.mark.asyncio
async def test_batch_arguments_cannot_mix_with_single_lookup_arguments(
    temp_db: HubDatabase, storage: LocalSkillManager
) -> None:
    storage.create_skill(name="present", description="p", content="present body\n")
    tool = _get_skill(temp_db)

    mixed = await tool(names=["present"], name="present")
    levels_without_names = await tool(name="present", levels={"present": "max"})

    assert mixed["error_code"] == "invalid_request"
    assert levels_without_names["error_code"] == "invalid_request"


@pytest.mark.asyncio
async def test_content_change_between_pages_invalidates_the_batch_cursor(
    temp_db: HubDatabase, storage: LocalSkillManager
) -> None:
    storage.create_skill(name="first", description="f", content="first line\n" * 800)
    second = storage.create_skill(name="second", description="s", content="second line\n" * 800)
    tool = _get_skill(temp_db)

    page_one = await tool(names=["first", "second"])
    storage.update_skill(second.id, content="changed\n")
    stale = await tool(cursor=page_one["page"]["next_cursor"])

    assert stale["success"] is False
    assert stale["error_code"] == "stale_cursor"


@pytest.mark.asyncio
async def test_batch_cursor_is_refused_by_the_file_tool(
    temp_db: HubDatabase, storage: LocalSkillManager
) -> None:
    from gobby.mcp_proxy.tools.skills import create_skills_registry

    storage.create_skill(name="first", description="f", content="first line\n" * 800)
    storage.create_skill(name="second", description="s", content="second line\n" * 800)
    registry = create_skills_registry(temp_db)
    batch_tool = registry.get_tool("get_skill")
    file_tool = registry.get_tool("get_skill_file")
    assert batch_tool is not None and file_tool is not None
    page_one = await batch_tool(names=["first", "second"])

    response = await file_tool(cursor=page_one["page"]["next_cursor"])

    assert response["error_code"] == "invalid_cursor"

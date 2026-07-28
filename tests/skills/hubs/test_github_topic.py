"""Contract tests for the SHA-pinned GitHub topic hub."""

from __future__ import annotations

import asyncio
import io
import tarfile
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gobby.config.skills import HubConfig
from gobby.mcp_proxy.tools.skills import create_skills_registry
from gobby.skills.hubs.base import DownloadResult
from gobby.skills.hubs.github_topic import GitHubTopicProvider
from gobby.skills.hubs.manager import HubManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager

SHA_A = "a" * 40
SHA_B = "b" * 40


def _skill_md(name: str, marker: str) -> bytes:
    return (
        f"---\nname: {name}\ndescription: {marker}\nversion: 1.0.0\n---\n\n# {marker}\n"
    ).encode()


def _archive(
    repo: str,
    sha: str,
    files: dict[str, bytes],
    *,
    extra_members: int = 0,
    link_kind: bytes | None = None,
) -> bytes:
    buffer = io.BytesIO()
    root = f"{repo.replace('/', '-')}-{sha[:7]}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative_path, content in files.items():
            info = tarfile.TarInfo(f"{root}/{relative_path}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        for index in range(extra_members):
            info = tarfile.TarInfo(f"{root}/noise/{index}.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        if link_kind is not None:
            info = tarfile.TarInfo(f"{root}/skills/demo/escape")
            info.type = link_kind
            info.linkname = "../../outside"
            archive.addfile(info)
    return buffer.getvalue()


class GitHubScenario:
    def __init__(self) -> None:
        self.repos: list[str] = []
        self.branch_sha: dict[str, str] = {}
        self.skills: dict[tuple[str, str], dict[str, bytes]] = {}
        self.archives: dict[tuple[str, str], bytes | int] = {}
        self.tree_override: dict[tuple[str, str], list[dict[str, object]]] = {}
        self.search_status = 200
        self.requests: list[httpx.Request] = []

    def add_repo(self, repo: str, sha: str, skills: dict[str, bytes]) -> None:
        self.repos.append(repo)
        self.branch_sha[repo] = sha
        self.skills[(repo, sha)] = skills
        self.archives[(repo, sha)] = _archive(repo, sha, skills)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/search/repositories":
            if self.search_status != 200:
                return httpx.Response(self.search_status, request=request)
            return httpx.Response(
                200,
                json={
                    "items": [{"full_name": repo, "default_branch": "main"} for repo in self.repos]
                },
                request=request,
            )
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[0] != "repos":
            return httpx.Response(404, request=request)
        repo = f"{parts[1]}/{parts[2]}"
        if parts[3] == "commits":
            sha = self.branch_sha.get(repo)
            return (
                httpx.Response(200, json={"sha": sha}, request=request)
                if sha
                else httpx.Response(404, request=request)
            )
        if parts[3:5] == ["git", "trees"]:
            sha = parts[5]
            skills = self.skills.get((repo, sha))
            if skills is None:
                return httpx.Response(404, request=request)
            tree = self.tree_override.get(
                (repo, sha),
                [
                    {"path": path, "type": "blob", "size": len(content)}
                    for path, content in skills.items()
                ],
            )
            return httpx.Response(200, json={"tree": tree, "truncated": False}, request=request)
        if parts[3] == "contents":
            skill_file = "/".join(parts[4:])
            sha = request.url.params.get("ref", "")
            content = self.skills.get((repo, sha), {}).get(skill_file)
            return (
                httpx.Response(200, content=content, request=request)
                if content is not None
                else httpx.Response(404, request=request)
            )
        if parts[3] == "tarball":
            sha = parts[4]
            archive = self.archives.get((repo, sha), 404)
            return (
                httpx.Response(archive, request=request)
                if isinstance(archive, int)
                else httpx.Response(200, content=archive, request=request)
            )
        return httpx.Response(404, request=request)


def _client_patch(scenario: GitHubScenario) -> AbstractContextManager[object]:
    transport = httpx.MockTransport(scenario.handler)
    return patch("httpx.AsyncClient", partial(httpx.AsyncClient, transport=transport))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sha_pinned_identity(tmp_path: Path) -> None:
    scenario = GitHubScenario()
    item_id = "acme/example.repo:skills/demo"
    scenario.add_repo("acme/example.repo", SHA_A, {"skills/demo/SKILL.md": _skill_md("demo", "A")})
    provider = GitHubTopicProvider("gobby-topic", "", topic="gobby-skill")

    with _client_patch(scenario):
        discovery = await provider.discover()
        scenario.branch_sha["acme/example.repo"] = SHA_B
        details = await provider.get_skill_details(item_id)
        download = await provider.download_skill(item_id, target_dir=str(tmp_path / "download"))

    assert discovery["records"] == [
        {"item_id": item_id, "repo": "acme/example.repo", "path": "skills/demo", "sha": SHA_A}
    ]
    assert details is not None and details.version == SHA_A
    assert download.success is True
    assert download.version == SHA_A
    assert download.provenance == {
        "item_id": item_id,
        "repo": "acme/example.repo",
        "path": "skills/demo",
        "sha": SHA_A,
    }
    assert (tmp_path / "download" / "SKILL.md").read_bytes() == _skill_md("demo", "A")
    assert sum("/commits/" in request.url.path for request in scenario.requests) == 1
    assert all(
        request.url.params.get("ref", SHA_A) == SHA_A
        for request in scenario.requests
        if "/contents/" in request.url.path
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_duplicate_slugs_and_moving_branch(tmp_path: Path) -> None:
    scenario = GitHubScenario()
    scenario.add_repo("one/skills", SHA_A, {"tools/lint/SKILL.md": _skill_md("lint", "one")})
    scenario.add_repo("two/skills", SHA_B, {"tools/lint/SKILL.md": _skill_md("lint", "two")})
    provider = GitHubTopicProvider("gobby-topic", "")

    with _client_patch(scenario):
        await provider.discover()
        scenario.branch_sha["one/skills"] = "c" * 40
        skills = await provider.list_skills()
        first = await provider.download_skill(
            "one/skills:tools/lint", target_dir=str(tmp_path / "one")
        )
        second = await provider.download_skill(
            "two/skills:tools/lint", target_dir=str(tmp_path / "two")
        )

    assert {skill.slug for skill in skills} == {
        "one/skills:tools/lint",
        "two/skills:tools/lint",
    }
    assert first.version == SHA_A
    assert second.version == SHA_B
    assert (tmp_path / "one" / "SKILL.md").read_bytes() == _skill_md("lint", "one")
    assert (tmp_path / "two" / "SKILL.md").read_bytes() == _skill_md("lint", "two")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_caps_rate_limit_and_disappearing_repo(tmp_path: Path) -> None:
    now = [0.0]
    scenario = GitHubScenario()
    scenario.add_repo("acme/skills", SHA_A, {"skills/demo/SKILL.md": _skill_md("demo", "safe")})
    provider = GitHubTopicProvider(
        "gobby-topic",
        "",
        cache_ttl_seconds=10,
        clock=lambda: now[0],
        sleep=AsyncMock(),
    )

    with _client_patch(scenario):
        first = await provider.discover()
        now[0] = 20
        scenario.search_status = 403
        cached = await provider.discover()
        scenario.search_status = 200
        scenario.archives[("acme/skills", SHA_A)] = 404
        unavailable = await provider.download_skill("acme/skills:skills/demo")
        for name, archive in {
            "symlink": _archive(
                "acme/skills",
                SHA_A,
                {"skills/demo/SKILL.md": _skill_md("demo", "safe")},
                link_kind=tarfile.SYMTYPE,
            ),
            "hardlink": _archive(
                "acme/skills",
                SHA_A,
                {"skills/demo/SKILL.md": _skill_md("demo", "safe")},
                link_kind=tarfile.LNKTYPE,
            ),
            "members": _archive(
                "acme/skills",
                SHA_A,
                {"skills/demo/SKILL.md": _skill_md("demo", "safe")},
                extra_members=513,
            ),
            "ratio": _archive(
                "acme/skills", SHA_A, {"skills/demo/SKILL.md": b"x" * (2 * 1024 * 1024)}
            ),
        }.items():
            scenario.archives[("acme/skills", SHA_A)] = archive
            target = tmp_path / name
            result = await provider.download_skill(
                "acme/skills:skills/demo", target_dir=str(target)
            )
            assert result.success is False
            assert not target.exists()
        scenario.tree_override[("acme/skills", SHA_A)] = [
            {"path": f"noise/{index}", "type": "blob", "size": 1} for index in range(201)
        ]
        fresh_provider = GitHubTopicProvider("fresh", "")
        skipped = await fresh_provider.discover()
        scenario.tree_override[("acme/skills", SHA_A)] = [
            {
                "path": f"skills/skill-{index}/SKILL.md",
                "type": "blob",
                "size": 1,
            }
            for index in range(51)
        ]
        over_skill_cap = await GitHubTopicProvider("over-skill-cap", "").discover()

    assert cached == first
    assert unavailable.success is False
    assert unavailable.error == "item_unavailable"
    assert skipped["records"] == []
    assert over_skill_cap["records"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_install_skill_topic_reference_end_to_end(temp_db: HubDatabase) -> None:
    scenario = GitHubScenario()
    item_id = "acme/example.repo:skills/demo"
    scenario.add_repo("acme/example.repo", SHA_A, {"skills/demo/SKILL.md": _skill_md("demo", "A")})
    manager = HubManager({"gobby-topic": HubConfig(type="github-topic", topic="gobby-skill")})
    manager.register_provider_factory("github-topic", GitHubTopicProvider)
    provider = manager.get_provider("gobby-topic")
    tool = create_skills_registry(temp_db, hub_manager=manager).get_tool("install_skill")

    with _client_patch(scenario):
        await provider.discover()
        scenario.branch_sha["acme/example.repo"] = SHA_B
        installed = await tool(source=f"gobby-topic:{item_id}")
        missing = await tool(source="gobby-topic:acme/example.repo:skills/missing")
        traversal = await tool(source="gobby-topic:acme/example.repo:skills/../demo")

    assert installed["success"] is True, installed
    stored = LocalSkillManager(temp_db).get_by_name("demo")
    assert stored is not None
    assert stored.source_path == item_id
    assert stored.source_ref == SHA_A
    assert missing["success"] is False and "item_unavailable" in missing["error"]
    assert traversal["success"] is False and "invalid" in traversal["error"].lower()
    assert sum("/commits/" in request.url.path for request in scenario.requests) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_discover_single_flights_concurrent_refreshes() -> None:
    scenario = GitHubScenario()
    scenario.add_repo(
        "acme/example.repo",
        SHA_A,
        {"skills/demo/SKILL.md": _skill_md("demo", "A")},
    )
    provider = GitHubTopicProvider("gobby-topic", "", topic="gobby-skill")

    with _client_patch(scenario):
        first, second = await asyncio.gather(provider.discover(), provider.discover())

    assert first == second
    assert sum(request.url.path == "/search/repositories" for request in scenario.requests) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_discover_skips_repository_with_invalid_json_shape() -> None:
    scenario = GitHubScenario()
    scenario.add_repo(
        "acme/example.repo",
        SHA_A,
        {"skills/demo/SKILL.md": _skill_md("demo", "A")},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "/commits/" in request.url.path:
            return httpx.Response(200, json=[])
        return scenario.handler(request)

    transport = httpx.MockTransport(handler)
    provider = GitHubTopicProvider("gobby-topic", "", topic="gobby-skill")
    with patch("httpx.AsyncClient", partial(httpx.AsyncClient, transport=transport)):
        discovery = await provider.discover()

    assert discovery["records"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_install_skill_rejects_mismatched_download_provenance(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    item_id = "acme/example.repo:skills/demo"
    manager = HubManager({"gobby-topic": HubConfig(type="github-topic", topic="gobby-skill")})
    manager.register_provider_factory("github-topic", GitHubTopicProvider)
    provider = manager.get_provider("gobby-topic")
    tool = create_skills_registry(temp_db, hub_manager=manager).get_tool("install_skill")
    assert tool is not None
    skill_dir = tmp_path / "download"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_bytes(_skill_md("demo", "A"))
    with patch.object(
        provider,
        "download_skill",
        new=AsyncMock(
            return_value=DownloadResult(
                success=True,
                slug="demo",
                path=str(skill_dir),
                version=SHA_A,
                provenance={
                    "item_id": item_id,
                    "repo": "other/repository",
                    "path": "skills/demo",
                    "sha": SHA_A,
                },
            )
        ),
    ):
        result = await tool(source=f"gobby-topic:{item_id}")

    assert result["success"] is False
    assert "item_unavailable" in result["error"]
    assert LocalSkillManager(temp_db).get_by_name("demo") is None

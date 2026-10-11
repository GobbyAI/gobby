"""Imported skill bodies and reference files share the content guardrail."""

import hashlib
from functools import partial
from pathlib import Path

import httpx
import pytest

from gobby.skills.hubs.claude_plugins import ClaudePluginsProvider
from gobby.skills.hubs.github_collection import GitHubCollectionProvider
from gobby.skills.hubs.github_topic import DiscoveryRecord, GitHubTopicProvider
from gobby.skills.loader import GitHubRef, SkillLoader
from gobby.skills.parser import ParsedSkill

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("load_all", [True, False])
def test_github_checkout_is_cleaned_without_rewriting_files(tmp_path: Path, load_all: bool) -> None:
    skill_dir = tmp_path / "sample"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    raw = "---\nname: sample\ndescription: desc\n---\nbody\u202e\U000e0061\n"
    skill_file.write_text(raw, encoding="utf-8")
    references = skill_dir / "references"
    references.mkdir()
    (references / "guide.md").write_text("reference\u200b", encoding="utf-8")
    result = SkillLoader()._load_github_checkout(
        GitHubRef(owner="owner", repo="repo", path=None),
        tmp_path if load_all else skill_dir,
        validate=False,
        load_all=load_all,
    )
    skill = result[0] if isinstance(result, list) else result
    assert isinstance(skill, ParsedSkill)
    assert skill.content == "body"
    assert skill.source_type == "github"
    assert skill.loaded_files is not None
    reference = skill.loaded_files[0]
    assert reference.content == "reference"
    assert reference.size_bytes == 9
    assert reference.content_hash == hashlib.sha256(b"reference").hexdigest()
    assert skill_file.read_text(encoding="utf-8") == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["collection", "plugins", "topic"])
async def test_hub_web_content_is_cleaned(provider: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="---\nname: sample\ndescription: desc\u202e\n---\nbody\U000e0061"
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", partial(client_type, transport=httpx.MockTransport(respond))
    )
    if provider == "collection":
        collection = GitHubCollectionProvider("hub", "", repo="owner/repo")
        content = await collection._fetch_skill_content("sample")
        assert content == "---\nname: sample\ndescription: desc\n---\nbody"
    elif provider == "plugins":
        plugins = ClaudePluginsProvider("hub", "https://claude-plugins.dev")
        content = await plugins._download_raw_file(
            "https://raw.githubusercontent.com/owner/repo/skill"
        )
        assert content == "---\nname: sample\ndescription: desc\n---\nbody"
    else:
        topic = GitHubTopicProvider("hub", "")
        record = DiscoveryRecord("owner/repo:sample", "owner/repo", "sample", "a" * 40)
        topic._records[record.item_id] = record
        topic._cache_expires_at = float("inf")
        details = await topic.get_skill_details(record.item_id)
        assert details is not None
        assert details.description == "desc"

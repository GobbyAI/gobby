"""Unit tests for session knowledge-synthesis (wiki) generation.

These cover the post-LLM pipeline (tag extraction, frontmatter wrapping,
redaction, persist, file mirror) and the skip policy without a live database:
the prompt render is monkeypatched and the LLM + session manager are fakes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.sessions import wiki_synthesis
from gobby.sessions.wiki_synthesis import (
    assemble_wiki_page,
    generate_session_wiki,
    is_wiki_markdown_valid,
    redact_wiki_markdown,
    resolve_wiki_file_path,
    wiki_generation_skip_reason,
)

pytestmark = pytest.mark.unit

_HOME = str(Path.home())

# Synthesis the (faked) LLM returns: suggested-tags comment, five sections,
# wikilinks, and secrets/home path that MUST be redacted before persistence.
_FIXTURE_SYNTHESIS = f"""<!-- suggested-tags: prompt-caching, rust, indexing -->

## Summary

Worked on prompt caching with [[PromptCache]] and [[RustIndexer]]. Stored the
key sk-ant-api03-SECRETKEYvalue1234567890 under {_HOME}/.gobby/config.

## Key Claims

- [[BM25]] ranking improved recall.
- The api_key=supersecretvalue123456 was rotated mid-session.

## Key Quotes

> "We set bearer abcdef0123456789ABCDEFGHIJ in the header" -- from the session

## Connections

- [[PromptCache]] -- central to the work

## Contradictions

- Contradicts [[OldDesign]] on: caching strategy
"""

_DIGEST = "### Turn 1\nfirst\n### Turn 2\nsecond\n### Turn 3\nthird"


def _make_session(**overrides: Any) -> SimpleNamespace:
    base = {
        "id": "169fe279-7e82-402e-ba6c-fe3f26ac8a57",
        "external_id": "f0a56143deadbeef",
        "source": "claude-code",
        "project_id": "gobby-cli",
        "model": "claude-opus-4-8",
        "created_at": "2026-06-03T12:00:00Z",
        "agent_depth": 0,
        "usage_input_tokens": 100,
        "usage_output_tokens": 50,
        "digest_markdown": _DIGEST,
        "wiki_markdown": None,
        "wiki_source_context_hash": None,
        "wiki_digest_turn_count": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeLLM:
    def __init__(self, output: str) -> None:
        self.output = output
        self.prompts: list[str] = []

    async def call_feature(
        self,
        feature_config: Any,
        prompt: str,
        *,
        system_prompt: str | None = None,
        caller: str | None = None,
        cwd: str | None = None,
    ) -> str:
        self.prompts.append(prompt)
        return self.output


class _FakeSessionManager:
    def __init__(self) -> None:
        self.db = None
        self.persist_calls: list[tuple[str, dict[str, Any]]] = []

    def persist_wiki_state(self, session_id: str, **kwargs: Any) -> Any:
        self.persist_calls.append((session_id, kwargs))
        return SimpleNamespace(id=session_id, wiki_markdown=kwargs.get("wiki_markdown"))


def _config(**overrides: Any) -> SimpleNamespace:
    base = {
        "enabled": True,
        "prompt_path": "wiki/source_page",
        "wiki_file_path": ".gobby/session_wiki",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def _stub_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        wiki_synthesis,
        "_render_wiki_prompt",
        lambda **kwargs: "RENDERED WIKI PROMPT",
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_extract_suggested_tags_splits_comment_from_body() -> None:
    tags, body = wiki_synthesis._extract_suggested_tags(_FIXTURE_SYNTHESIS)
    assert tags == ["prompt-caching", "rust", "indexing"]
    assert body.startswith("## Summary")
    assert "suggested-tags" not in body


def test_redact_wiki_markdown_scrubs_secrets_and_home_path() -> None:
    redacted = redact_wiki_markdown(_FIXTURE_SYNTHESIS)
    assert "sk-ant-api03-SECRETKEYvalue1234567890" not in redacted
    assert "SECRETKEY" not in redacted
    assert "supersecretvalue123456" not in redacted
    assert "abcdef0123456789ABCDEFGHIJ" not in redacted
    assert _HOME not in redacted
    # Structure survives redaction.
    assert "## Summary" in redacted
    assert "[[PromptCache]]" in redacted


def test_is_wiki_markdown_valid_requires_core_sections() -> None:
    assert is_wiki_markdown_valid("## Summary\nx\n## Key Claims\n- y")
    assert not is_wiki_markdown_valid("## Summary\nonly summary")
    assert not is_wiki_markdown_valid("")
    assert not is_wiki_markdown_valid(None)


def test_assemble_wiki_page_wraps_frontmatter_and_redacts() -> None:
    session = _make_session()
    tags, body, page = assemble_wiki_page(session, _FIXTURE_SYNTHESIS)

    assert tags == ["prompt-caching", "rust", "indexing"]
    assert body.startswith("## Summary")
    # Frontmatter (the seam) wraps the body exactly once.
    assert page.startswith("---\n")
    assert page.count("\n---\n") == 1
    assert "title: 'Session: f0a56143" in page or 'title: "Session: f0a56143' in page
    assert "type: source" in page
    assert "tags:" in page
    assert "- prompt-caching" in page
    assert "- rust" in page
    assert "- indexing" in page
    assert "session_id: 169fe279-7e82-402e-ba6c-fe3f26ac8a57" in page
    assert "source: claude-code" in page
    # Redaction applied to the assembled page.
    assert "SECRETKEY" not in page
    assert _HOME not in page


def test_resolve_wiki_file_path_strips_leading_gobby(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    session = _make_session()
    path = resolve_wiki_file_path(session, _config())
    assert path == tmp_path / "session_wiki" / "f0a56143deadbeef.md"


def test_resolve_wiki_file_path_honors_absolute(tmp_path: Path) -> None:
    session = _make_session()
    abs_dir = tmp_path / "custom_wiki"
    path = resolve_wiki_file_path(session, _config(wiki_file_path=str(abs_dir)))
    assert path == abs_dir / "f0a56143deadbeef.md"


def test_resolve_wiki_file_path_sanitizes_session_identifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    session = _make_session(external_id="../bad/path")
    path = resolve_wiki_file_path(session, _config())
    assert path == tmp_path / "session_wiki" / "bad-path.md"


# --------------------------------------------------------------------------- #
# Skip policy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"digest_markdown": ""}, "no_digest"),
        ({"agent_depth": 1}, "subagent"),
        ({"source": "pipeline"}, "ephemeral_source:pipeline"),
        ({"source": "cron"}, "ephemeral_source:cron"),
        ({"digest_markdown": "### Turn 1\nonly one"}, "below_digest_threshold"),
        ({}, None),
    ],
)
def test_wiki_generation_skip_reason(overrides: dict[str, Any], expected: str | None) -> None:
    session = _make_session(**overrides)
    assert wiki_generation_skip_reason(session, session.digest_markdown) == expected


# --------------------------------------------------------------------------- #
# generate_session_wiki orchestration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_generate_session_wiki_disabled_returns_skip() -> None:
    result = await generate_session_wiki(
        session=_make_session(),
        digest_markdown=_DIGEST,
        session_manager=_FakeSessionManager(),
        llm_service=_FakeLLM(_FIXTURE_SYNTHESIS),
        session_wiki_config=_config(enabled=False),
    )
    assert result == {"generated": False, "skipped": "disabled"}


@pytest.mark.asyncio
async def test_generate_session_wiki_no_llm_returns_skip() -> None:
    result = await generate_session_wiki(
        session=_make_session(),
        digest_markdown=_DIGEST,
        session_manager=_FakeSessionManager(),
        llm_service=None,
        session_wiki_config=_config(),
    )
    assert result == {"generated": False, "skipped": "no_llm_service"}


@pytest.mark.asyncio
async def test_generate_session_wiki_subagent_skips() -> None:
    result = await generate_session_wiki(
        session=_make_session(agent_depth=2),
        digest_markdown=_DIGEST,
        session_manager=_FakeSessionManager(),
        llm_service=_FakeLLM(_FIXTURE_SYNTHESIS),
        session_wiki_config=_config(),
    )
    assert result == {"generated": False, "skipped": "subagent"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_persists_and_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    session = _make_session()
    manager = _FakeSessionManager()
    llm = _FakeLLM(_FIXTURE_SYNTHESIS)

    result = await generate_session_wiki(
        session=session,
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=llm,
        session_wiki_config=_config(),
    )

    assert result["generated"] is True
    assert result["tags"] == ["prompt-caching", "rust", "indexing"]
    assert result["digest_turn_count"] == 3
    assert result["persisted"] is True

    # File mirror written under gobby_home, redacted, frontmatter + five sections.
    wiki_file = tmp_path / "session_wiki" / "f0a56143deadbeef.md"
    assert wiki_file.exists()
    file_content = wiki_file.read_text(encoding="utf-8")
    for section in (
        "## Summary",
        "## Key Claims",
        "## Key Quotes",
        "## Connections",
        "## Contradictions",
    ):
        assert section in file_content
    assert "[[PromptCache]]" in file_content
    assert "SECRETKEY" not in file_content
    assert "supersecretvalue123456" not in file_content
    assert _HOME not in file_content

    # DB column and file are the SAME redacted body (no divergence on secrets).
    assert len(manager.persist_calls) == 1
    session_id, kwargs = manager.persist_calls[0]
    assert session_id == session.id
    assert kwargs["wiki_markdown"] == file_content
    assert kwargs["generation_mode"] == "full"
    assert kwargs["digest_turn_count"] == 3
    assert kwargs["source_context_hash"] == result["source_context_hash"]
    assert kwargs["source_context_hash"] == wiki_synthesis.wiki_source_context_hash(
        session=session,
        digest_markdown=_DIGEST,
        prompt_path="wiki/source_page",
        rendered_prompt="RENDERED WIKI PROMPT",
    )
    assert kwargs["wiki_path"] == str(wiki_file)
    assert "SECRETKEY" not in kwargs["wiki_markdown"]
    assert _HOME not in kwargs["wiki_markdown"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_noop_on_hash_match() -> None:
    session = _make_session()
    # Pre-seed the session with the hash the generator will compute.
    expected_hash = wiki_synthesis.wiki_source_context_hash(
        session=session,
        digest_markdown=_DIGEST,
        prompt_path="wiki/source_page",
        rendered_prompt="RENDERED WIKI PROMPT",
    )
    session.wiki_source_context_hash = expected_hash
    session.wiki_digest_turn_count = 3
    session.wiki_markdown = "## Summary\nexisting\n## Key Claims\n- a"

    manager = _FakeSessionManager()
    result = await generate_session_wiki(
        session=session,
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM(_FIXTURE_SYNTHESIS),
        session_wiki_config=_config(),
    )

    assert result["generated"] is False
    assert result["skipped"] == "noop"
    assert manager.persist_calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_empty_synthesis_skips() -> None:
    manager = _FakeSessionManager()
    result = await generate_session_wiki(
        session=_make_session(),
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM("   "),
        session_wiki_config=_config(),
    )
    assert result == {"generated": False, "skipped": "empty_synthesis"}
    assert manager.persist_calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_invalid_synthesis_skips_without_fallback() -> None:
    manager = _FakeSessionManager()
    # Missing the required sections -> invalid, and there is no fallback.
    result = await generate_session_wiki(
        session=_make_session(),
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM("just some prose with no sections"),
        session_wiki_config=_config(),
    )
    assert result == {"generated": False, "skipped": "invalid_synthesis"}
    assert manager.persist_calls == []

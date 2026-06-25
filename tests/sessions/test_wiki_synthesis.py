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
    def __init__(self, output: str | Exception) -> None:
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
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


class _FakeSessionManager:
    def __init__(self) -> None:
        self.db = None
        self.persist_calls: list[tuple[str, dict[str, Any]]] = []
        self.failure_calls: list[tuple[str, str, str | None]] = []

    def persist_wiki_state(self, session_id: str, **kwargs: Any) -> Any:
        self.persist_calls.append((session_id, kwargs))
        return SimpleNamespace(id=session_id, wiki_markdown=kwargs.get("wiki_markdown"))

    def record_wiki_synthesis_failure(
        self,
        session_id: str,
        reason: str,
        error: str | None = None,
    ) -> Any:
        self.failure_calls.append((session_id, reason, error))
        return SimpleNamespace(id=session_id)


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
    assert is_wiki_markdown_valid(
        "# Session\n\n## Summary\nReal summary text.\n\n## Key Claims\n- Real claim text."
    )
    assert is_wiki_markdown_valid(
        "### Summary ###\nSummary body.\n\n#### Detail\nNested detail.\n\n### Key Claims\nClaim body."
    )
    assert not is_wiki_markdown_valid("## Summary\nonly summary")
    assert not is_wiki_markdown_valid("Summary and Key Claims are words in prose.")
    assert not is_wiki_markdown_valid("## Summary\n\n## Key Claims\nClaim body.")
    assert not is_wiki_markdown_valid("## Summary\n- \n\n## Key Claims\n** **")
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
    assert manager.failure_calls == []


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
    assert manager.failure_calls == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_empty_synthesis_skips() -> None:
    manager = _FakeSessionManager()
    session = _make_session()
    result = await generate_session_wiki(
        session=session,
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM("   "),
        session_wiki_config=_config(),
    )
    assert result == {"generated": False, "skipped": "empty_synthesis"}
    assert manager.persist_calls == []
    assert manager.failure_calls == [(session.id, "empty_synthesis", None)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_invalid_synthesis_skips_without_fallback() -> None:
    manager = _FakeSessionManager()
    session = _make_session()
    # Missing the required sections -> invalid, and there is no fallback.
    result = await generate_session_wiki(
        session=session,
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM("Summary and Key Claims are only prose mentions."),
        session_wiki_config=_config(),
    )
    assert result == {"generated": False, "skipped": "invalid_synthesis"}
    assert manager.persist_calls == []
    assert manager.failure_calls == [(session.id, "invalid_synthesis", None)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_llm_error_records_failure() -> None:
    manager = _FakeSessionManager()
    session = _make_session()
    result = await generate_session_wiki(
        session=session,
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM(RuntimeError("provider unavailable")),
        session_wiki_config=_config(),
    )

    assert result == {
        "generated": False,
        "skipped": "llm_error",
        "error": "provider unavailable",
    }
    assert manager.persist_calls == []
    assert manager.failure_calls == [(session.id, "llm_error", "provider unavailable")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_prompt")
async def test_generate_session_wiki_empty_required_section_records_invalid_failure() -> None:
    manager = _FakeSessionManager()
    session = _make_session()
    result = await generate_session_wiki(
        session=session,
        digest_markdown=_DIGEST,
        session_manager=manager,
        llm_service=_FakeLLM("## Summary\n\n## Key Claims\n- claim"),
        session_wiki_config=_config(),
    )

    assert result == {"generated": False, "skipped": "invalid_synthesis"}
    assert manager.persist_calls == []
    assert manager.failure_calls == [(session.id, "invalid_synthesis", None)]


# ---------------------------------------------------------------------------
# backfill_session_wikis — batched historical backfill from stored digests.
# ---------------------------------------------------------------------------

_VALID_WIKI = "## Summary\n\nReal summary body text.\n\n## Key Claims\n\n- A real claim.\n"


class _FakeBackfillManager:
    """Session-manager stub exposing get_wiki_backfill_candidates over a list."""

    def __init__(self, sessions: list[Any]) -> None:
        self.db = None
        self._sessions = sessions

    def get_wiki_backfill_candidates(
        self, *, limit: int | None = None, after_id: str | None = None
    ) -> list[Any]:
        items = [s for s in self._sessions if after_id is None or s.id > after_id]
        return items[:limit] if limit is not None else items


def _patch_generate(
    monkeypatch: pytest.MonkeyPatch,
    results: list[Any] | dict[str, Any] | Exception,
) -> list[str]:
    """Patch summarize.generate_session_summaries; return its call-id log.

    ``results`` is either a single canned return value / Exception reused for
    every call, or a list consumed one per call.
    """
    calls: list[str] = []
    queue = list(results) if isinstance(results, list) else None

    async def _fake_gen(*, session_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(session_id)
        outcome = queue.pop(0) if queue is not None else results
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    import gobby.sessions.summarize as summarize_mod

    monkeypatch.setattr(summarize_mod, "generate_session_summaries", _fake_gen)
    return calls


async def _run_backfill(manager: Any, **kwargs: Any) -> wiki_synthesis.WikiBackfillResult:
    return await wiki_synthesis.backfill_session_wikis(
        session_manager=manager,
        llm_service=object(),
        session_summary_config=_config(),
        session_wiki_config=_config(),
        db=None,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_backfill_dry_run_counts_without_synthesizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        _make_session(id="s-00", wiki_markdown=None),  # eligible (missing wiki)
        _make_session(id="s-01", wiki_markdown=_VALID_WIKI),  # already valid → skip
        _make_session(id="s-02", source="pipeline"),  # ephemeral → skip
    ]
    calls = _patch_generate(monkeypatch, {"success": True, "wiki": {"generated": True}})

    result = await _run_backfill(_FakeBackfillManager(sessions), dry_run=True)

    assert result.scanned == 3
    assert result.eligible == 1
    assert result.skipped == 2
    assert result.synthesized == 0
    assert result.attempts == 0
    assert calls == []  # dry-run never calls the trigger


@pytest.mark.asyncio
async def test_backfill_synthesizes_missing_wiki(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [_make_session(id="s-00", wiki_markdown=None)]
    calls = _patch_generate(
        monkeypatch, {"success": True, "wiki": {"generated": True, "wiki_length": 321}}
    )

    result = await _run_backfill(_FakeBackfillManager(sessions))

    assert calls == ["s-00"]
    assert result.synthesized == 1
    assert result.failed == 0
    assert result.eligible == 1


@pytest.mark.asyncio
async def test_backfill_skips_already_valid_wiki(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [_make_session(id="s-00", wiki_markdown=_VALID_WIKI)]
    calls = _patch_generate(monkeypatch, {"success": True, "wiki": {"generated": True}})

    result = await _run_backfill(_FakeBackfillManager(sessions))

    assert calls == []  # valid wiki → no synthesis attempt (idempotent re-run)
    assert result.skipped == 1
    assert result.attempts == 0


@pytest.mark.asyncio
async def test_backfill_noop_skip_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [_make_session(id="s-00", wiki_markdown=None)]
    _patch_generate(monkeypatch, {"success": True, "wiki": {"generated": False, "skipped": "noop"}})

    result = await _run_backfill(_FakeBackfillManager(sessions))

    assert result.synthesized == 0
    assert result.failed == 0
    assert result.skipped == 1


@pytest.mark.asyncio
async def test_backfill_wiki_error_counts_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [_make_session(id="s-00", wiki_markdown=None)]
    _patch_generate(
        monkeypatch,
        {
            "success": True,
            "wiki": {"generated": False, "skipped": "llm_error", "error": "boom"},
        },
    )

    result = await _run_backfill(_FakeBackfillManager(sessions))

    assert result.synthesized == 0
    assert result.failed == 1
    assert result.failures[0].session_id == "s-00"
    assert result.failures[0].reason == "llm_error"
    assert result.failures[0].error == "boom"


@pytest.mark.asyncio
async def test_backfill_summary_failure_counts_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [_make_session(id="s-00", wiki_markdown=None)]
    _patch_generate(monkeypatch, {"success": False, "error": "No session found"})

    result = await _run_backfill(_FakeBackfillManager(sessions))

    assert result.failed == 1
    assert result.failures[0].reason == "summary_error"
    assert result.failures[0].error == "No session found"


@pytest.mark.asyncio
async def test_backfill_one_failure_does_not_abort_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        _make_session(id="s-00", wiki_markdown=None),
        _make_session(id="s-01", wiki_markdown=None),
    ]
    calls = _patch_generate(
        monkeypatch,
        [RuntimeError("kaboom"), {"success": True, "wiki": {"generated": True}}],
    )

    result = await _run_backfill(_FakeBackfillManager(sessions))

    assert calls == ["s-00", "s-01"]  # batch continued past the first failure
    assert result.failed == 1
    assert result.synthesized == 1
    assert result.failures[0].reason == "exception"


@pytest.mark.asyncio
async def test_backfill_limit_bounds_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [_make_session(id=f"s-{i:02d}", wiki_markdown=None) for i in range(5)]
    calls = _patch_generate(monkeypatch, {"success": True, "wiki": {"generated": True}})

    result = await _run_backfill(_FakeBackfillManager(sessions), limit=2)

    assert len(calls) == 2
    assert result.attempts == 2
    assert result.synthesized == 2


@pytest.mark.asyncio
async def test_backfill_limit_reaches_attempts_past_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        _make_session(id="s-00", wiki_markdown=_VALID_WIKI),
        _make_session(id="s-01", wiki_markdown=_VALID_WIKI),
        _make_session(id="s-02", wiki_markdown=None),
        _make_session(id="s-03", wiki_markdown=None),
        _make_session(id="s-04", wiki_markdown=None),
    ]
    calls = _patch_generate(monkeypatch, {"success": True, "wiki": {"generated": True}})

    result = await _run_backfill(_FakeBackfillManager(sessions), limit=2)

    assert calls == ["s-02", "s-03"]  # skipped 2 valid, attempted 2 eligible
    assert result.attempts == 2
    assert result.skipped == 2


@pytest.mark.asyncio
async def test_backfill_dry_run_limit_caps_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [_make_session(id=f"s-{i:02d}", wiki_markdown=None) for i in range(5)]
    calls = _patch_generate(monkeypatch, {"success": True, "wiki": {"generated": True}})

    result = await _run_backfill(_FakeBackfillManager(sessions), dry_run=True, limit=2)

    assert result.eligible == 2  # stops counting once the limit is reached
    assert calls == []

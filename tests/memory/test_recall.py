"""Tests for daemon-owned memory recall selection."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from gobby.config.feature_base import FeatureProfile
from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.memory.recall import MemoryRecallRunner, is_memory_recall_eligible
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

PROJECT_ID = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
EXTERNAL_SESSION_ID = "external-memory-recall"
MACHINE_ID = "test-machine"


class FakeMemoryManager:
    def __init__(self, memories: list[Memory] | None = None, error: Exception | None = None):
        self.memories = memories or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def search_memories(self, **kwargs: Any) -> list[Memory]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.memories


class FakeLLMService:
    def __init__(self, response: Any | None = None, responses: list[Any] | None = None):
        self.response = {"memory_ids": []} if response is None else response
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        caller: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "caller": caller,
            }
        )
        if self.error is not None:
            raise self.error
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return self.response


class LegacyLLMService:
    def __init__(self, response: str = '{"memory_ids":["mem-1"]}'):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def call_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        *,
        caller: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "max_tokens": max_tokens,
                "caller": caller,
            }
        )
        return self.response


def _event(
    *,
    event_type: HookEventType = HookEventType.BEFORE_AGENT,
    source: SessionSource = SessionSource.CLAUDE,
    prompt: str = "please use project memory to fix this regression today",
    metadata: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    event_data = {"prompt": prompt}
    if data:
        event_data.update(data)
    return HookEvent(
        event_type=event_type,
        session_id=EXTERNAL_SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=event_data,
        project_id=PROJECT_ID,
        metadata={"_platform_session_id": SESSION_ID, **(metadata or {})},
    )


def _memory(
    memory_id: str,
    content: str = "Useful project convention",
    *,
    similarity: Any = 0.91,
    tags: list[str] | None = None,
) -> Memory:
    return Memory(
        id=memory_id,
        memory_type="fact",
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        project_id=PROJECT_ID,
        tags=tags if tags is not None else ["test"],
        similarity=similarity,
        search_via="semantic",
    )


def _variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "parent_turn_seq": 3,
        "is_spawned_agent": False,
    }
    variables.update(overrides)
    return variables


@pytest.fixture
def _persisted_recall_session(temp_db: HubDatabase) -> None:
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (PROJECT_ID, "memory-recall-test"),
    )
    temp_db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            SESSION_ID,
            EXTERNAL_SESSION_ID,
            MACHINE_ID,
            SessionSource.CLAUDE.value,
            PROJECT_ID,
        ),
    )


@pytest.mark.parametrize(
    ("event", "variables", "expected"),
    [
        (_event(), _variables(), True),
        (_event(prompt="too short"), _variables(), False),
        (_event(source=SessionSource.PIPELINE), _variables(), False),
        (_event(event_type=HookEventType.AFTER_AGENT), _variables(), False),
        (_event(metadata={"synthetic": True}), _variables(), False),
        (_event(metadata={"_platform_session_id": ""}), _variables(), False),
        (_event(data={"actor": "daemon"}), _variables(), False),
        (
            _event(prompt="Message from Gobby daemon: New activity available."),
            _variables(),
            False,
        ),
        (_event(), _variables(is_spawned_agent=True), False),
        (_event(), _variables(parent_turn_seq=None), False),
    ],
)
def test_recall_eligibility_only_accepts_real_parent_user_turns(
    event: HookEvent,
    variables: dict[str, Any],
    expected: bool,
) -> None:
    assert is_memory_recall_eligible(event, variables, MemoryRecallConfig()) is expected


@pytest.mark.parametrize(
    "source",
    [
        SessionSource.AGY,
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
    ],
)
def test_recall_eligibility_accepts_real_parent_prompts_from_supported_sources(
    source: SessionSource,
) -> None:
    event = _event(source=source, metadata={"role": "user", "prompt_kind": "user"})

    assert is_memory_recall_eligible(event, _variables(), MemoryRecallConfig()) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "AGENTS.md instructions for /Users/josh/Projects/gobby\n\n# Personality\nStay concise.",
        "# AGENTS.md instructions for /Users/josh/Projects/gobby\n\n<INSTRUCTIONS>\nRules here.",
        "<codex_internal_context><cwd>/tmp</cwd></codex_internal_context>",
        "<turn_aborted>The user interrupted the previous turn.</turn_aborted>",
        "\n".join(
            [
                "<permissions instructions>",
                "Filesystem sandboxing defines which files can be read or written.",
                "</permissions instructions>",
                "<collaboration_mode>",
                "Known mode names are Default and Plan.",
                "</collaboration_mode>",
                "Gobby Session ID: #3426 (47bafb0e-c69c-440b-ba8f-890fab976145)",
                "",
                "## Instructions",
                "LIFECYCLE MODEL:",
            ]
        ),
        (
            "Continue where you last left off. Before continuing, call "
            '`gobby-sessions.wait_for_summary(session_id="s1")`. If it returns '
            "`completed=false`, repeat the same wait call. Once complete, use the "
            "returned `context` and continue."
        ),
        (
            "Continue where you last left off. If startup context contains "
            "`<!-- gobby:injected-context:begin -->`, use that injected context directly "
            "and continue. Only if the injected context is missing or incomplete, call "
            '`gobby-sessions.wait_for_summary(session_id="s1")`. If it returns '
            "`completed=false`, repeat the same wait call. Once complete, use the "
            "returned `context` and continue."
        ),
        "Task #123 has incomplete subtasks. Use suggest_next_task() and continue working.",
    ],
)
def test_recall_eligibility_rejects_unmarked_protocol_prompt_bodies(prompt: str) -> None:
    assert (
        is_memory_recall_eligible(_event(prompt=prompt), _variables(), MemoryRecallConfig())
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _event(metadata={"prompt_kind": "protocol"}),
        _event(metadata={"role": "system"}),
        _event(data={"prompt_type": "wait"}),
        _event(prompt="<turn_aborted>The user interrupted the previous turn.</turn_aborted>"),
    ],
)
async def test_runner_skips_synthetic_prompts_without_search(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
    event: HookEvent,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager([_memory("mem-1")])
    llm = FakeLLMService({"memory_ids": ["mem-1"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    assert await runner.run(event, SESSION_ID, _variables()) is None
    assert memory_manager.calls == []
    assert llm.calls == []


@pytest.mark.asyncio
async def test_runner_selects_memory_with_json_feature_call_and_no_child_session(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("mem-1", "Use gcode before broad source reads."),
            _memory("mem-2", "Unrelated but plausible."),
        ]
    )
    llm = FakeLLMService({"memory_ids": ["mem-1", "mem-1", "mem-2"]})
    config = MemoryRecallConfig(candidate_limit=8, selected_limit=1, min_score=0.7)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=config,
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert payload.origin_turn_seq == 3
    UUID(payload.recall_request_id)
    assert payload.memories == [
        {
            "id": "mem-1",
            "content": "Use gcode before broad source reads.",
            "type": "fact",
            "created_at": "2026-01-01T00:00:00+00:00",
            "tags": ["test"],
            "similarity": 0.91,
            "search_via": "semantic",
        }
    ]
    assert memory_manager.calls == [
        {
            "query": "please use project memory to fix this regression today",
            "project_id": PROJECT_ID,
            "limit": 8,
            "min_score": 0.7,
            "tags_none": ["review-lesson"],
            "session_id": SESSION_ID,
            "recall_request_id": payload.recall_request_id,
            "caller": "memory.recall",
        }
    ]
    assert llm.calls[0]["caller"] == "memory.recall"
    assert llm.calls[0]["feature_config"] is config
    assert LocalAgentRunManager(temp_db).list_by_parent(SESSION_ID) == []


@pytest.mark.asyncio
async def test_runner_synthesizes_large_prompt_query_before_search(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    long_prompt = (
        "please recall the memory for task gobby-123 about prompt origin " * 20
        + "UNSEARCHABLE_RAW_CONTEXT_TOKEN " * 20
    )
    memory_manager = FakeMemoryManager([_memory("mem-1", "Prompt origin recall fix.")])
    llm = FakeLLMService(
        responses=[
            {"query": "gobby-123 memory recall prompt origin"},
            {"memory_ids": ["mem-1"]},
        ]
    )
    config = MemoryRecallConfig(
        profile=FeatureProfile.HIGH,
        candidates=["claude/sonnet"],
        query_synthesis_threshold=80,
        query_max_chars=64,
    )
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=config,
    )

    payload = await runner.run(_event(prompt=long_prompt), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["mem-1"]
    assert memory_manager.calls[0]["query"] == "gobby-123 memory recall prompt origin"
    assert llm.calls[0]["caller"] == "memory.recall.query"
    assert llm.calls[0]["feature_config"].profile == FeatureProfile.LOW
    assert llm.calls[0]["feature_config"].candidates != config.candidates
    assert llm.calls[1]["caller"] == "memory.recall"
    assert llm.calls[1]["feature_config"] is config
    assert "gobby-123 memory recall prompt origin" in llm.calls[1]["prompt"]
    assert "UNSEARCHABLE_RAW_CONTEXT_TOKEN" not in llm.calls[1]["prompt"]


@pytest.mark.asyncio
async def test_runner_shares_timeout_between_query_synthesis_and_selection(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowQueryLLM(FakeLLMService):
        async def call_json_feature(
            self,
            feature_config: Any,
            prompt: str,
            system_prompt: str | None = None,
            *,
            caller: str | None = None,
        ) -> Any:
            self.calls.append(
                {
                    "feature_config": feature_config,
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    "caller": caller,
                }
            )
            if caller == "memory.recall.query":
                return {"query": "delayed query"}
            return {"memory_ids": ["mem-1"]}

    times = [100.0, 100.1, 100.2, 100.3, 100.4, 101.1, 101.2, 101.3]

    def fake_monotonic() -> float:
        if times:
            return times.pop(0)
        return 101.3

    monkeypatch.setattr("gobby.memory.recall.time.monotonic", fake_monotonic)
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    config = MemoryRecallConfig(
        profile=FeatureProfile.HIGH,
        candidates=["claude/sonnet"],
        query_synthesis_threshold=1,
        timeout=1,
    )
    llm = SlowQueryLLM()
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=config,
    )

    payload = await runner.run(
        _event(prompt="recall this memory please " * 20),
        SESSION_ID,
        _variables(),
    )

    assert payload is None
    assert [call["caller"] for call in llm.calls] == ["memory.recall.query"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_response",
    [
        TimeoutError("query synthesis timed out"),
        RuntimeError("query synthesis failed"),
        {"query": ""},
    ],
)
async def test_runner_falls_back_when_large_prompt_query_synthesis_fails(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
    query_response: Any,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    long_prompt = (
        "AlphaMemorySpecific BetaMemorySpecific GammaMemorySpecific DeltaMemorySpecific "
        + "copied boilerplate text " * 20
    )
    memory_manager = FakeMemoryManager([_memory("mem-1", "Fallback target.")])
    llm = FakeLLMService(responses=[query_response, {"memory_ids": []}])
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(query_synthesis_threshold=80, query_max_chars=72),
    )

    payload = await runner.run(_event(prompt=long_prompt), SESSION_ID, _variables())

    assert payload is None
    assert memory_manager.calls[0]["query"].startswith(
        "AlphaMemorySpecific BetaMemorySpecific GammaMemorySpecific"
    )
    assert len(memory_manager.calls[0]["query"]) <= 72
    assert llm.calls[0]["caller"] == "memory.recall.query"
    assert llm.calls[1]["caller"] == "memory.recall"


@pytest.mark.asyncio
async def test_runner_excludes_review_lessons_from_prompt_recall(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    raw_review_lesson = "# Review Lesson: Raw diagnostic should not be prompted"
    memory_manager = FakeMemoryManager(
        [
            _memory("review-1", raw_review_lesson, tags=["review-lesson", "confirmed"]),
            _memory("mem-1", "Use task-linked commits."),
        ]
    )
    llm = FakeLLMService({"memory_ids": ["review-1", "mem-1"]})
    config = MemoryRecallConfig(candidate_limit=8, selected_limit=2, min_score=0.7)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=config,
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["mem-1"]
    assert raw_review_lesson not in llm.calls[0]["prompt"]
    assert memory_manager.calls[0]["tags_none"] == ["review-lesson"]


@pytest.mark.asyncio
async def test_runner_filters_low_and_nonnumeric_scores_but_keeps_keyword_hits(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    """Keyword/RRF hits carry similarity=None and must reach the selector (#17772)."""
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("weak", "Weak match should stay out.", similarity=0.42),
            _memory("keyword", "Keyword-ranked hit must pass through.", similarity=None),
            _memory("nonnumeric", "Nonnumeric score should stay out.", similarity="high"),
            _memory("strong", "Strong match should reach recall.", similarity=0.91),
        ]
    )
    llm = FakeLLMService({"memory_ids": ["weak", "keyword", "nonnumeric", "strong"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(candidate_limit=8, selected_limit=3, min_score=0.7),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["keyword", "strong"]
    prompt = llm.calls[0]["prompt"]
    assert '"strong"' in prompt
    assert '"keyword"' in prompt
    assert '"weak"' not in prompt
    assert '"nonnumeric"' not in prompt


@pytest.mark.asyncio
async def test_runner_keyword_only_candidates_reach_recall(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    """A pure-keyword result set (semantic outage or degraded search) still delivers."""
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("kw-1", "First keyword hit.", similarity=None),
            _memory("kw-2", "Second keyword hit.", similarity=None),
        ]
    )
    llm = FakeLLMService({"memory_ids": ["kw-2"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(candidate_limit=8, selected_limit=3),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["kw-2"]
    assert payload.memories[0].get("similarity") is None


@pytest.mark.asyncio
async def test_runner_logs_funnel_skip_reasons_at_info(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Post-eligibility skip reasons must be observable at INFO level (#17772)."""
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([]),  # type: ignore[arg-type]
        llm_service=FakeLLMService({"memory_ids": []}),
        config=MemoryRecallConfig(),
    )

    with caplog.at_level(logging.INFO, logger="gobby.memory.recall"):
        payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is None
    skip_records = [r for r in caplog.records if "no_candidate_memories" in r.getMessage()]
    assert skip_records
    assert skip_records[0].levelno == logging.INFO
    assert "recall_request_id=" in skip_records[0].getMessage()


@pytest.mark.asyncio
async def test_runner_logs_ineligible_prompt_skips_at_debug(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-eligibility skips fire on every turn and must stay below INFO."""
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=FakeLLMService({"memory_ids": ["mem-1"]}),
        config=MemoryRecallConfig(),
    )
    with caplog.at_level(logging.DEBUG, logger="gobby.memory.recall"):
        payload = await runner.run(_event(prompt="too short"), SESSION_ID, _variables())

    assert payload is None
    skip_records = [r for r in caplog.records if "prompt_too_short" in r.getMessage()]
    assert skip_records
    assert all(r.levelno == logging.DEBUG for r in skip_records)


@pytest.mark.asyncio
async def test_runner_filters_already_injected_duplicates_before_llm(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    sv_mgr = SessionVariableManager(temp_db)
    sv_mgr.set_variable(SESSION_ID, "parent_turn_seq", 3)
    sv_mgr.set_variable(SESSION_ID, "injected_memory_ids", ["mem-1"])
    llm = FakeLLMService({"memory_ids": ["mem-2"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1"), _memory("mem-2")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["mem-2"]
    assert '"mem-1"' not in llm.calls[0]["prompt"]
    assert '"mem-2"' in llm.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_runner_skips_legacy_feature_only_service(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    llm = LegacyLLMService()
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is None
    assert llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_manager", "llm", "expected_llm_calls"),
    [
        (FakeMemoryManager([]), FakeLLMService({"memory_ids": ["mem-1"]}), 0),
        (FakeMemoryManager([_memory("mem-1")]), FakeLLMService("not-json"), 1),
        (FakeMemoryManager([_memory("mem-1")]), FakeLLMService({"memory_ids": "mem-1"}), 1),
    ],
)
async def test_runner_safe_empty_candidates_and_invalid_json(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
    memory_manager: FakeMemoryManager,
    llm: FakeLLMService,
    expected_llm_calls: int,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is None
    assert len(llm.calls) == expected_llm_calls


@pytest.mark.asyncio
async def test_runner_safe_when_llm_fails(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    llm = FakeLLMService()
    llm.error = RuntimeError("llm unavailable")
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    assert await runner.run(_event(), SESSION_ID, _variables()) is None


@pytest.mark.asyncio
async def test_runner_drops_stale_turn_result(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    sv_mgr = SessionVariableManager(temp_db)
    sv_mgr.set_variable(SESSION_ID, "parent_turn_seq", 4)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=FakeLLMService({"memory_ids": ["mem-1"]}),
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables(parent_turn_seq=3))

    assert payload is None


async def test_runner_deferred_mode_keeps_result_after_turn_advances(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    sv_mgr = SessionVariableManager(temp_db)
    sv_mgr.set_variable(SESSION_ID, "parent_turn_seq", 4)
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=FakeMemoryManager([_memory("mem-1")]),  # type: ignore[arg-type]
        llm_service=FakeLLMService({"memory_ids": ["mem-1"]}),
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(
        _event(),
        SESSION_ID,
        _variables(parent_turn_seq=3),
        require_same_turn=False,
    )

    assert payload is not None
    assert payload.origin_turn_seq == 3
    assert [memory["id"] for memory in payload.memories] == ["mem-1"]


@pytest.mark.asyncio
async def test_runner_records_selection_outcomes_for_non_selected(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    """Returned-but-not-selected candidates get durable filtered rows (§5)."""
    from gobby.config.persistence import MemoryConfig

    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager(
        [
            _memory("mem-1", "Selected memory."),
            _memory("mem-2", "Not selected."),
            _memory("mem-3", "Review lesson.", tags=["review-lesson"]),
        ]
    )
    memory_manager.config = MemoryConfig(recall_signal_hub=True)  # type: ignore[attr-defined]
    llm = FakeLLMService({"memory_ids": ["mem-1"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    assert [memory["id"] for memory in payload.memories] == ["mem-1"]
    rows = temp_db.fetchall(
        "SELECT memory_id, outcome, drop_reason, drop_detail, turn_seq, caller "
        "FROM recall_injection_outcomes WHERE recall_request_id = %s",
        (payload.recall_request_id,),
    )
    by_id = {row["memory_id"]: row for row in rows}
    # The selected memory's outcome is written later, at the delivery chain.
    assert set(by_id) == {"mem-2", "mem-3"}
    assert all(row["outcome"] == "filtered" for row in rows)
    assert by_id["mem-2"]["drop_reason"] == "other"
    assert by_id["mem-2"]["drop_detail"] == "selector_not_selected"
    assert by_id["mem-3"]["drop_reason"] == "review_lesson"
    assert all(row["turn_seq"] == 3 for row in rows)
    assert all(row["caller"] == "memory.recall" for row in rows)


@pytest.mark.asyncio
async def test_runner_records_no_outcomes_when_hub_flag_off(
    temp_db: HubDatabase,
    _persisted_recall_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "parent_turn_seq", 3)
    memory_manager = FakeMemoryManager([_memory("mem-1"), _memory("mem-2")])
    llm = FakeLLMService({"memory_ids": ["mem-1"]})
    runner = MemoryRecallRunner(
        db=temp_db,
        memory_manager=memory_manager,  # type: ignore[arg-type]
        llm_service=llm,
        config=MemoryRecallConfig(),
    )

    payload = await runner.run(_event(), SESSION_ID, _variables())

    assert payload is not None
    count = temp_db.fetchone("SELECT count(*) AS n FROM recall_injection_outcomes")
    assert count is not None and count["n"] == 0

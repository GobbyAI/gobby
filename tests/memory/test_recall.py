"""Tests for substantive parent-prompt memory recall."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.memory import generation_schemas
from gobby.memory import recall as recall_module
from gobby.memory.recall import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TERMS,
    RECALL_DIGEST_TAIL_CHARS,
    REVIEW_LESSON_TAG,
    MemoryRecallRunner,
    PromptDecisionKind,
    RecallSessionState,
    scrub_memory_recall_query,
)
from gobby.review_learning.fingerprint import build_occurrence_key
from gobby.review_learning.lessons import normalize_lesson
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

PROJECT_ID = "44444444-4444-4444-8444-444444444444"
SESSION_ID = "55555555-5555-4555-8555-555555555555"
EXTERNAL_SESSION_ID = "external-memory-recall"


class FakeMemoryConfig:
    """Stand-in for `MemoryConfig` with the durable outcome writer switched on."""

    recall_signal_hub = True


class FakeMemoryManager:
    def __init__(
        self,
        memories: list[Memory] | None = None,
        error: Exception | None = None,
        *,
        record_outcomes: bool = False,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ):
        self.memories = memories or []
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.config = FakeMemoryConfig() if record_outcomes else None
        self.read_error = read_error
        self.write_error = write_error
        self.db_calls: list[str] = []
        self.db_results: list[Any] = []

    async def search_memories(self, **kwargs: Any) -> list[Memory]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return [memory for memory in self.memories if _passes_tags_none(memory, kwargs)]

    async def run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Mirror `MemoryManager.run_db`: every call leaves the event loop thread."""
        name = getattr(func, "__name__", "")
        self.db_calls.append(name)
        failure = self.read_error if name == _BATCHED_READ else self.write_error
        if failure is not None:
            raise failure
        result = await asyncio.to_thread(func, *args, **kwargs)
        self.db_results.append(result)
        return result


_BATCHED_READ = "_read_session_state"


class EventLoopThreadDbCall(BaseException):
    """Escapes recall's fail-open handlers so a loop-thread DB call cannot hide."""


class LoopThreadGuardDb:
    """Database proxy that fails any call made on the event loop thread."""

    def __init__(self, inner: HubDatabase, loop_thread_id: int) -> None:
        self._inner = inner
        self._loop_thread_id = loop_thread_id

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if threading.get_ident() == self._loop_thread_id:
                raise EventLoopThreadDbCall(f"{name} ran on the daemon event loop thread")
            return attribute(*args, **kwargs)

        return guarded


def _passes_tags_none(memory: Memory, kwargs: dict[str, Any]) -> bool:
    """Mirror the `tags_none` drop `build_results` applies to every hybrid hit."""
    excluded = kwargs.get("tags_none") or []
    return not any(tag in (memory.tags or []) for tag in excluded)


def _event(
    prompt: str = "Implement the parser fix and add focused tests for the failing path.",
    *,
    event_type: HookEventType = HookEventType.BEFORE_AGENT,
    source: SessionSource = SessionSource.CLAUDE,
    metadata: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    event_data = {"prompt": prompt, **(data or {})}
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
    content: str = "Useful project convention.",
    *,
    similarity: float | None = 0.91,
    tags: list[str] | None = None,
) -> Memory:
    return Memory(
        id=memory_id,
        memory_type="fact",
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        project_id=PROJECT_ID,
        tags=tags or ["test"],
        similarity=similarity,
        search_via="hybrid",
    )


def _variables(**overrides: Any) -> dict[str, Any]:
    return {"parent_turn_seq": 3, "is_spawned_agent": False, **overrides}


def _runner(db: HubDatabase, manager: FakeMemoryManager) -> MemoryRecallRunner:
    return MemoryRecallRunner(
        db=db,
        memory_manager=manager,  # type: ignore[arg-type]
        config=MemoryRecallConfig(),
    )


@pytest.fixture
def persisted_session(temp_db: HubDatabase) -> None:
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
            "21000000-0000-4000-8000-000000000002",
            SessionSource.CLAUDE.value,
            PROJECT_ID,
        ),
    )


@pytest.mark.parametrize(
    ("event", "variables"),
    [
        (_event("ok"), _variables()),
        (_event("approved"), _variables()),
        (_event("continue"), _variables()),
        (_event("what is the status?"), _variables()),
        (_event("are you done?"), _variables()),
        (_event("status update please"), _variables()),
        (_event("wait"), _variables()),
        (_event("load the python skill"), _variables()),
        (_event("compact this session"), _variables()),
        (_event("/gobby help"), _variables()),
        (_event(metadata={"synthetic": True}), _variables()),
        (_event(), _variables(is_spawned_agent=True)),
        (_event(source=SessionSource.PIPELINE), _variables()),
        (_event(event_type=HookEventType.AFTER_AGENT), _variables()),
    ],
)
@pytest.mark.asyncio
async def test_hard_skips_reach_no_search(
    temp_db: HubDatabase,
    event: HookEvent,
    variables: dict[str, Any],
) -> None:
    manager = FakeMemoryManager([_memory("m1")])

    result = await _runner(temp_db, manager).run(event, SESSION_ID, variables)

    assert result is None
    assert manager.calls == []


@pytest.mark.asyncio
async def test_prompt_past_hard_skip_runs_one_hybrid_search(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager([_memory(f"m{index}") for index in range(1, 6)])

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["m1", "m2", "m3"]
    assert len(manager.calls) == 1
    assert manager.calls[0]["caller"] == "memory.recall"
    assert manager.calls[0]["tags_none"] == ["review-lesson"]


def test_runner_takes_no_llm_service() -> None:
    """No LLM call can occur on the recall path: the runner holds no LLM service."""
    parameters = inspect.signature(MemoryRecallRunner.__init__).parameters

    assert "llm_service" not in parameters
    assert not hasattr(MemoryRecallRunner, "_classify")


def test_prompt_decision_kind_keeps_only_hard_skip() -> None:
    assert [kind.value for kind in PromptDecisionKind] == ["hard_skip"]


def test_recall_classification_schema_is_gone() -> None:
    """1.1.4: the classifier's JSON schema has no remaining references."""
    assert not hasattr(generation_schemas, "RECALL_CLASSIFICATION_SCHEMA")
    assert "RECALL_CLASSIFICATION_SCHEMA" not in generation_schemas.__all__


@pytest.mark.asyncio
async def test_short_prompt_past_hard_skip_reaches_search(temp_db: HubDatabase) -> None:
    """The deleted heuristic rejected this prompt; hard-skip alone now lets it through."""
    manager = FakeMemoryManager([_memory("m1")])

    result = await _runner(temp_db, manager).run(
        _event("Which lane should this go in?"),
        SESSION_ID,
        _variables(),
    )

    assert result is not None
    assert len(manager.calls) == 1


def test_scrubber_considers_full_prompt_and_preserves_technical_tail() -> None:
    prompt = " ".join(f"ordinary{index}" for index in range(160))
    prompt += " investigate src/gobby/memory/recall.py ParserError --strict final_marker"

    query = scrub_memory_recall_query(prompt)

    assert len(query) <= MAX_QUERY_CHARS
    assert len(query.split()) <= MAX_QUERY_TERMS
    assert "src/gobby/memory/recall.py" in query
    assert "ParserError" in query
    assert "--strict" in query
    assert "final_marker" in query


@pytest.mark.asyncio
async def test_filters_duplicates_and_injected_ids_in_rank_order(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "injected_memory_ids", ["m2"])
    manager = FakeMemoryManager(
        [
            _memory("m1"),
            _memory("m1"),
            _memory("m2"),
            _memory("m3", similarity=None),
        ]
    )
    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == [
        "m1",
        "m3",
    ]


def test_review_lesson_tag_matches_what_lessons_are_written_with() -> None:
    """1.2.1: the recall exclusion tag is the tag the lesson writer stamps."""
    lesson = normalize_lesson(
        source_kind="review_comment",
        source="coderabbit",
        source_review="review-1",
        decision="confirmed",
        finding={"title": "Use psycopg placeholders", "severity": "high"},
        evidence={"commit": "abc123"},
        finding_fingerprint="native-1",
        occurrence_key=build_occurrence_key("review-1", "native-1"),
        repo="josh/gobby",
        language="python",
        risk="high",
    )

    assert REVIEW_LESSON_TAG in lesson.tags


@pytest.mark.asyncio
async def test_review_lessons_excluded_from_prompt_recall(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """1.2.3: the search layer drops review lessons, so recall never sees one.

    The lesson carries the literal tag `build_tags` stamps, not the constant, so a
    constant that drifts from the writer lets the lesson through.
    """
    manager = FakeMemoryManager(
        [
            _memory("lesson", tags=["review-lesson", "confirmed"]),
            _memory("m1"),
        ]
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["m1"]
    assert manager.calls[0]["tags_none"] == ["review-lesson"]


def test_filter_ranked_no_longer_carries_a_review_lesson_branch() -> None:
    """1.2.2: the drop is unreachable once the tag is right, so the branch is gone."""
    assert not hasattr(recall_module, "_has_review_lesson_tag")
    assert "review_lesson" not in inspect.getsource(MemoryRecallRunner._filter_ranked)


@pytest.mark.asyncio
async def test_candidates_below_min_score_floor_are_dropped(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """The default floor (p10 of the logged distribution) trims low-signal hits."""
    floor = MemoryRecallConfig().min_score
    assert floor == 0.45
    manager = FakeMemoryManager(
        [
            _memory("strong", similarity=0.6),
            _memory("weak", similarity=floor - 0.01),
            _memory("unscored", similarity=None),
        ]
    )
    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["strong", "unscored"]


@pytest.mark.asyncio
async def test_search_failure_allows_turn_to_continue(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager(error=RuntimeError("search unavailable"))

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_recall_runs_every_database_call_off_the_event_loop(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """1.3.1: recall's DB work belongs to the executor, never to the daemon loop.

    The guard raises a `BaseException`, so recall's fail-open `except Exception`
    handlers cannot turn a loop-thread call into a silently degraded turn.
    """
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "injected_memory_ids", ["m2"])
    guarded = cast(HubDatabase, LoopThreadGuardDb(temp_db, threading.get_ident()))
    manager = FakeMemoryManager([_memory("m1"), _memory("m2")], record_outcomes=True)

    result = await _runner(guarded, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["m1"]
    assert manager.db_calls[0] == _BATCHED_READ
    assert len(manager.db_calls) == 2, "the ledger read and the outcome write both go off-loop"


@pytest.mark.asyncio
async def test_one_batched_read_serves_the_ledger_and_the_digest_slice(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """1.3.2: one executor round trip carries both, so 2.2 adds no second trip."""
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "injected_memory_ids", ["m2"])
    temp_db.execute(
        "UPDATE sessions SET last_turn_markdown = %s WHERE id = %s",
        ("Previous turn digest slice.", SESSION_ID),
    )
    manager = FakeMemoryManager([_memory("m1"), _memory("m2")])

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["m1"]
    reads = [state for state in manager.db_results if isinstance(state, RecallSessionState)]
    assert len(reads) == 1
    assert reads[0].injected_memory_ids == frozenset({"m2"})
    assert reads[0].last_turn_markdown == "Previous turn digest slice."


@pytest.mark.asyncio
async def test_batched_read_failure_injects_nothing(temp_db: HubDatabase) -> None:
    """1.3.3: a turn with no dedupe ledger and no digest slice injects nothing."""
    manager = FakeMemoryManager(
        [_memory("m1")],
        read_error=RuntimeError("database executor queue is full"),
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert manager.db_calls == [_BATCHED_READ]


@pytest.mark.asyncio
async def test_outcome_write_failure_preserves_delivery(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """1.3.4: outcome capture is telemetry and never retracts what the turn delivered."""
    manager = FakeMemoryManager(
        [_memory(f"m{index}") for index in range(1, 5)],
        record_outcomes=True,
        write_error=RuntimeError("outcome writer is down"),
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["m1", "m2", "m3"]
    assert manager.db_calls[1:] == ["record"], "the rank-limit drop attempted one write"


@pytest.mark.asyncio
async def test_substantive_prompt_is_embedded_as_natural_language(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.2.1: BM25 keeps the scrubbed bag; the vector side gets the sentence."""
    prompt = (
        "Refactor the dispatcher so worktree cleanup happens before "
        "the merge stage records its commit."
    )
    manager = FakeMemoryManager([_memory("m1")])

    await _runner(temp_db, manager).run(_event(prompt), SESSION_ID, _variables())

    call = manager.calls[0]
    assert call.get("embed_text") == prompt
    assert call["query"] != prompt, "the scrubbed term bag still drives BM25"


@pytest.mark.asyncio
async def test_thin_query_enriched_with_bounded_digest_tail(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.2.3: a thin bag borrows the previous turn's tail to embed against."""
    digest = "".join(f"Previous turn line {index} about the parser. " for index in range(200))
    temp_db.execute(
        "UPDATE sessions SET last_turn_markdown = %s WHERE id = %s",
        (digest, SESSION_ID),
    )
    manager = FakeMemoryManager([_memory("m1")])
    prompt = "fix the parser"

    await _runner(temp_db, manager).run(_event(prompt), SESSION_ID, _variables())

    embed_text = manager.calls[0].get("embed_text") or ""
    assert embed_text.startswith(prompt), "the prompt still leads the embedded query"
    tail = embed_text[len(prompt) :].strip()
    assert digest.strip().endswith(tail), "the enrichment is the tail of the previous turn"
    assert tail == digest[-RECALL_DIGEST_TAIL_CHARS:].strip(), "the tail is the bounded slice"
    assert 0 < len(tail) <= RECALL_DIGEST_TAIL_CHARS


@pytest.mark.asyncio
async def test_embed_text_respects_max_query_chars(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.2.4: the assembled query obeys the bound the scrubbed bag obeys."""
    unit = "Refactor the dispatcher module carefully. "
    manager = FakeMemoryManager([_memory("m1")])

    await _runner(temp_db, manager).run(_event(unit * 60), SESSION_ID, _variables())

    embed_text = manager.calls[0].get("embed_text") or ""
    assert 0 < len(embed_text) <= MAX_QUERY_CHARS
    assert "..." in embed_text, "the bound is the head-and-tail elision, not a hard cut"
    assert embed_text.startswith(unit.strip())
    assert embed_text.endswith(unit.strip())


@pytest.mark.asyncio
async def test_digest_tail_drops_previously_injected_context(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.2.5: recalled memory text must not feed back into the query that recalls it."""
    digest = (
        "<!-- gobby:injected-context:begin -->\nStale handoff notes.\n"
        "<!-- gobby:injected-context:end -->\n"
        "<project-memory>\nGobby prefers uv for every Python operation.\n</project-memory>\n"
        "The parser still drops the trailing newline."
    )
    temp_db.execute(
        "UPDATE sessions SET last_turn_markdown = %s WHERE id = %s",
        (digest, SESSION_ID),
    )
    manager = FakeMemoryManager([_memory("m1")])

    await _runner(temp_db, manager).run(_event("fix the parser"), SESSION_ID, _variables())

    embed_text = manager.calls[0].get("embed_text") or ""
    assert "project-memory" not in embed_text
    assert "uv for every Python operation" not in embed_text
    assert "Stale handoff notes" not in embed_text
    assert "The parser still drops the trailing newline." in embed_text


@pytest.mark.asyncio
async def test_digest_tail_is_the_slice_of_the_turn_not_a_cleaned_window(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.2.5: the enrichment is the bounded slice of the turn, then stripped.

    A `<project-memory>` block the slice cuts through leaves a closing tag with
    no opener. Dropping that orphan is the stripper's job; reaching further
    back than `RECALL_DIGEST_TAIL_CHARS` of the turn to refill the budget is
    not, because the bound is on the turn, not on what survives cleaning.
    """
    older = "".join(f"Alpha line {index} about the dispatcher. " for index in range(30))
    injected = "".join(f"Recalled memory {index} says use uv. " for index in range(20))
    recent = "".join(f"Bravo line {index} about the parser. " for index in range(5))
    digest = f"{older}<project-memory>\n{injected}\n</project-memory>\n{recent}"
    assert len(injected) > RECALL_DIGEST_TAIL_CHARS, "the block must span the slice boundary"
    temp_db.execute(
        "UPDATE sessions SET last_turn_markdown = %s WHERE id = %s",
        (digest, SESSION_ID),
    )
    manager = FakeMemoryManager([_memory("m1")])

    await _runner(temp_db, manager).run(_event("fix the parser"), SESSION_ID, _variables())

    embed_text = manager.calls[0].get("embed_text") or ""
    assert "Bravo line 4" in embed_text, "the tail of the turn survives"
    assert "project-memory" not in embed_text
    assert "Recalled memory" not in embed_text
    assert "Alpha line" not in embed_text, "cleaning must not widen the slice"

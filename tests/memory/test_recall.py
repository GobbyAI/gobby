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
from gobby.hooks.memory_recall_delivery import _memory_bodies
from gobby.mcp_proxy.tools.memory_recall import _next_chunk
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
    _memory_to_payload,
    scrub_memory_recall_query,
)
from gobby.memory.services._search_keyword import keyword_fallback
from gobby.memory.services._search_results import build_results
from gobby.review_learning.fingerprint import build_occurrence_key
from gobby.review_learning.lessons import normalize_lesson
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import Memory
from gobby.workflows.engine.delivery_formatting import _format_project_memory
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
    temporal_decay_factor: float | None = 1.0,
    search_via: str = "hybrid",
    tags: list[str] | None = None,
    rationale: str | None = None,
) -> Memory:
    """A search hit: `similarity` is the decayed score, undecayed by its decay factor.

    Decay defaults to 1.0, which makes the two axes the same number; pass a
    factor below 1 to age a candidate and put it on one side of the selection
    floor and the other side of the ranking order.
    """
    return Memory(
        id=memory_id,
        memory_type="fact",
        content=content,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        project_id=PROJECT_ID,
        tags=tags or ["test"],
        similarity=similarity,
        temporal_decay_factor=temporal_decay_factor,
        search_via=search_via,
        rationale=rationale,
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
            # Lower-ranked than m1 but still over the selection floor, so what
            # this test observes is dedupe and the ledger, never the floor.
            _memory("m3", similarity=0.70),
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


def _drop_row(db: HubDatabase, memory_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT drop_reason, drop_detail FROM recall_injection_outcomes WHERE memory_id = %s",
        (memory_id,),
    )
    assert row is not None, f"no injection outcome recorded for {memory_id}"
    return dict(row)


@pytest.mark.asyncio
async def test_selection_floor_can_yield_zero_memories(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.3.2: candidates the search floor admits can still all miss the selection floor.

    `collect_active_results` doubles the candidate pool until `limit` hits clear
    `min_score`, so a floor the backfill loop chases can never empty a turn. Only
    the independent selection floor can.
    """
    config = MemoryRecallConfig()
    manager = FakeMemoryManager(
        [
            _memory("near", similarity=config.selection_min_score - 0.01),
            _memory("far", similarity=config.min_score + 0.01),
        ]
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert manager.calls[0]["min_score"] == config.min_score, (
        "the search still runs on the search floor"
    )


@pytest.mark.asyncio
async def test_selection_floor_drop_detail(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.3.3: the floor drop is distinguishable from every other filtered row."""
    floor = MemoryRecallConfig().selection_min_score
    manager = FakeMemoryManager(
        [_memory("kept", similarity=floor + 0.01), _memory("dropped", similarity=floor - 0.01)],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["kept"]
    assert _drop_row(temp_db, "dropped") == {
        "drop_reason": "other",
        "drop_detail": "selection_min_score",
    }


@pytest.mark.asyncio
async def test_null_similarity_candidates_are_dropped(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """2.3.4: an unscored hit cannot be shown to clear the floor, so it never ships."""
    manager = FakeMemoryManager(
        [
            _memory("scored", similarity=MemoryRecallConfig().selection_min_score + 0.01),
            _memory("unscored", similarity=None),
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["scored"]
    assert _drop_row(temp_db, "unscored") == {
        "drop_reason": "other",
        "drop_detail": "null_similarity",
    }


@pytest.mark.asyncio
async def test_the_selection_floor_tests_the_undecayed_score(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """#20831: the last gate before the model is a relevance test, not an age test.

    `similarity` is `score * user_boost * temporal_decay`, so thresholding it
    made the floor a recency test wearing a relevance test's name -- at a 30-day
    half-life the decay factor is exactly 0.5, which demanded `score * boost >=
    1.30` to inject at all. The floor divides the decay back out; decay keeps
    ordering candidates. Both candidates here sit on the opposite side of each
    axis, so putting the decay back into the comparison inverts the result.
    """
    floor = MemoryRecallConfig().selection_min_score
    assert floor == 0.70, "the numbers below are chosen around this floor"
    manager = FakeMemoryManager(
        # 0.60 / 0.8 = 0.75 clears the floor; 0.60 alone does not.
        # 0.69 is under the floor on both axes, undecayed included.
        [
            _memory("aged-but-on-topic", similarity=0.60, temporal_decay_factor=0.8),
            _memory("fresh-but-off-topic", similarity=0.69, temporal_decay_factor=1.0),
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["aged-but-on-topic"]
    assert _drop_row(temp_db, "fresh-but-off-topic") == {
        "drop_reason": "other",
        "drop_detail": "selection_min_score",
    }


@pytest.mark.asyncio
async def test_a_graph_only_candidate_stays_eligible(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """#20831: the floor divides decay out rather than reading the raw cosine.

    Graph-synthetic hits are 27.8% of scored hits and carry no raw cosine at
    all, so a floor that read `raw_semantic_score` would have permanently
    disabled the recall expander (#17104) as a side effect of this fix. Their
    synthetic score is a real score on the undecayed axis and is admitted.
    """
    manager = FakeMemoryManager(
        # 0.65 / 0.9 = 0.72 clears the 0.70 floor; 0.65 alone does not.
        [
            _memory(
                "graph-synthetic",
                similarity=0.65,
                temporal_decay_factor=0.9,
                search_via="graph",
            )
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["graph-synthetic"]


@pytest.mark.asyncio
async def test_a_keyword_only_candidate_is_never_injected(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """No score at all, no injection -- the backstop, narrowed by #20873.

    The #20831 ruling was epistemic: a keyword hit carried no similarity, so
    nothing could divide the decay out of it and nothing could compare it to
    the floor. That premise holds only for a memory with no stored vector to
    score, which is what this pins. A keyword hit whose memory does carry a
    vector is judged at the threshold like every other candidate -- see
    `test_a_keyword_hit_with_a_stored_vector_is_judged_at_the_threshold`.
    """
    floor = MemoryRecallConfig().selection_min_score
    manager = FakeMemoryManager(
        [
            _memory("semantic", similarity=floor + 0.01),
            _memory("keyword-only", similarity=None, search_via="keyword"),
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["semantic"]
    assert _drop_row(temp_db, "keyword-only") == {
        "drop_reason": "other",
        "drop_detail": "null_similarity",
    }


class _FlatStorage:
    """Storage for building a candidate through the real `build_results`."""

    def __init__(self, memory_ids: list[str]) -> None:
        self._memory_ids = memory_ids

    def _memory(self, memory_id: str) -> Memory:
        if memory_id not in self._memory_ids:
            raise ValueError(memory_id)
        return _memory(memory_id, similarity=None, temporal_decay_factor=None)

    def get_memories(self, memory_ids: list[str], scope: Any = None) -> list[Memory]:
        return [self._memory(memory_id) for memory_id in memory_ids]

    def get_memory(self, memory_id: str, scope: Any = None) -> Memory:
        return self._memory(memory_id)


def _rescored(
    memory_id: str,
    *,
    cosine: float,
    keyword: bool = False,
    graph_confidence: float | None = None,
) -> Memory:
    """One search candidate, scored by the real `build_results`.

    A hand-built `Memory(similarity=...)` cannot observe what rescoring did,
    because rescoring is exactly the step that decides whether `similarity` is
    set at all (#20873 criterion 3). Decay is neutral here -- the fixture's
    memories are dated 2026-01-01 and the half-life is large enough that the
    undecayed and decayed axes stay the same number.
    """
    results = build_results(
        storage=cast(Any, _FlatStorage([memory_id])),
        merged_ids=[memory_id],
        ranking_score_map={memory_id: 0.5},
        # What `_score_unwindowed_candidates` fills in: a real cosine for any
        # candidate the collection can score, whichever leg surfaced it.
        qdrant_score_map={memory_id: cosine},
        qdrant_set=set(),
        keyword_set={memory_id} if keyword else set(),
        graph_set=set() if graph_confidence is None else {memory_id},
        graph_score_map=None if graph_confidence is None else {memory_id: graph_confidence},
        rrf_applied=False,
        project_id=None,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
        half_life=1.0e9,
        effective_min_score=0.0,
        limit=5,
    )
    assert len(results) == 1, "the fixture must produce exactly one candidate"
    return results[0]


@pytest.mark.asyncio
async def test_a_graph_expander_hit_is_injected_not_merely_admitted(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """Admission alone leaves the expander off where it matters (#20873 rider 2).

    If graph confidence gates only the search floor while the selection gate
    still judges the real cosine, a confidence-0.80 / cosine-0.40 hit reaches
    the result set and is never injected. Both gates read the same axis, so
    this asserts injection rather than survival.
    """
    candidate = _rescored("expander-find", cosine=0.40, graph_confidence=0.80)
    assert candidate.similarity is not None
    assert candidate.similarity < MemoryRecallConfig().selection_min_score, (
        "the fixture must put the real cosine under the selection floor"
    )
    # A control that always injects, so a recall that ran and skipped the
    # expander hit is distinguishable from a recall that produced nothing.
    manager = FakeMemoryManager(
        [_memory("control", similarity=0.91), candidate],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert sorted(memory["id"] for memory in result.memories) == ["control", "expander-find"]


@pytest.mark.asyncio
async def test_a_graph_expander_hit_under_the_confidence_floor_is_still_dropped(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """Confidence gates the selection axis too; it does not exempt from it.

    0.572 is the median of the measured 2026-08 confidence distribution, so
    this pins the ordinary expander find: it is admitted to search at 0.611 and
    still not injected at the 0.653 p90 seat.
    """
    manager = FakeMemoryManager(
        [
            _memory("control", similarity=0.91),
            _rescored("weak-link", cosine=0.40, graph_confidence=0.572),
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["control"]
    assert _drop_row(temp_db, "weak-link")["drop_detail"] == "graph_confidence_min_score"


@pytest.mark.asyncio
async def test_a_keyword_hit_with_a_stored_vector_is_judged_at_the_threshold(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """#20873: the same bar as everything else, once there is a score to judge.

    `_score_unwindowed_candidates` removes the premise the #20831 ruling rested
    on, so a keyword-found memory that carries a stored vector is judged at
    `selection_min_score`. No bar is lowered: the sub-threshold hit is still
    dropped, and by the threshold rather than by provenance.
    """
    floor = MemoryRecallConfig().selection_min_score
    manager = FakeMemoryManager(
        [
            _rescored("keyword-strong", cosine=floor + 0.05, keyword=True),
            _rescored("keyword-weak", cosine=floor - 0.05, keyword=True),
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    assert [memory["id"] for memory in result.memories] == ["keyword-strong"]
    assert _drop_row(temp_db, "keyword-weak") == {
        "drop_reason": "other",
        "drop_detail": "selection_min_score",
    }


class _NoVectorStorage:
    """`keyword_fallback` storage: every memory exists and none carries a score."""

    def get_memory(self, memory_id: str) -> Memory:
        return _memory(memory_id, similarity=None, temporal_decay_factor=None)


@pytest.mark.asyncio
async def test_a_no_vector_deployments_top_keyword_hit_is_not_injected(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """The fallback's max-normalized top hit is a rank, not a similarity (#20874).

    Keyword scores are max-normalized, so the best hit scores exactly 1.0
    whatever its relevance; carried as `similarity` it cleared the 0.70 floor
    unconditionally and injected every turn, against the invariant #20858
    pinned. Built through the real `keyword_fallback` because that producer is
    where the fabricated similarity came from: with no vector store there is
    genuinely no stored vector to score, which is exactly the case #20873
    narrowed the null-similarity backstop to, so every hit is dropped there.
    """

    async def run_storage(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    candidates = await keyword_fallback(
        run_storage=run_storage,
        storage=cast(Any, _NoVectorStorage()),
        keyword_search=lambda query, limit, project_id, *, include_global=True: [
            ("top-hit", 1.0),
            ("runner-up", 0.4),
        ],
        query="parser fix",
        limit=5,
        project_id=PROJECT_ID,
        memory_type=None,
        tags_all=None,
        tags_any=None,
        tags_none=None,
    )
    assert [memory.ranking_score for memory in candidates] == [1.0, 0.4]
    assert all(memory.similarity is None for memory in candidates)
    manager = FakeMemoryManager(candidates, record_outcomes=True)

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert _drop_row(temp_db, "top-hit") == {
        "drop_reason": "other",
        "drop_detail": "null_similarity",
    }


class _HangingManager(FakeMemoryManager):
    """Search that outlives any reasonable turn, recording its cancellation."""

    def __init__(self) -> None:
        super().__init__([])
        self.cancelled = False

    async def search_memories(self, **kwargs: Any) -> list[Memory]:
        self.calls.append(kwargs)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []


@pytest.mark.asyncio
async def test_recall_search_is_bounded_by_an_outer_deadline(
    temp_db: HubDatabase,
    persisted_session: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One wall-clock bound covers the whole search, backfill rounds included (#20874).

    The search's own timeouts are per leg -- 10s Qdrant search plus 10s rescore
    per round, across up to four backfill rounds -- so a slow-but-succeeding
    search could hold the user's turn for their whole stack. The runner bounds
    the call as a whole, cancels the in-flight search, and fails open to
    injecting nothing.
    """
    monkeypatch.setattr(recall_module, "RECALL_SEARCH_DEADLINE_SECONDS", 0.05)
    manager = _HangingManager()

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is None
    assert manager.cancelled is True


# The candidate set session d404a7c9-8f8c-4f4a-9f9a-... recorded for the query
# "What environment prefix do agent pytest runs need in this repo, and where
# does that value come from?", read back from recall_signal_hits and
# recall_injection_outcomes: (id, decayed similarity, decay factor). Under the
# decayed floor exactly one was injected, and it was the AgentDefinitionManager
# memory -- the one memory of the eight that had nothing to do with the query.
_SMOKE_CANDIDATES = [
    ("agent-definition-manager", 0.6663, 0.9381, "semantic"),
    ("database-url-test-protect", 0.5922, 0.8357, "semantic"),
    ("highest-raw-cosine", 0.5721, 0.7997, "semantic"),
    ("graph-keyword-synthetic", 0.5582, 0.9893, "graph|keyword"),
    ("keyword-only-1", None, None, "keyword"),
    ("keyword-only-2", None, None, "keyword"),
    ("keyword-only-3", None, None, "keyword"),
    ("keyword-only-4", None, None, "keyword"),
]


@pytest.mark.asyncio
async def test_the_recorded_smoke_candidates_now_inject_the_answer(
    temp_db: HubDatabase,
    persisted_session: None,
) -> None:
    """#20831 criterion 3: replay the candidate set that exposed the bug.

    The memory that actually answered the question ranked second and was
    dropped, while a memory about `AgentDefinitionManager` with a *lower*
    undecayed score was injected -- the only thing separating them was age.
    Undecayed, the three semantic hits are 0.7103, 0.7086 and 0.7154; all clear
    0.70 and the rank cap admits all three, so the answer ships. The
    graph-synthetic hit undecays to 0.5643 and is dropped on relevance, which is
    the floor doing its actual job.
    """
    manager = FakeMemoryManager(
        [
            _memory(
                memory_id,
                similarity=similarity,
                temporal_decay_factor=decay,
                search_via=search_via,
            )
            for memory_id, similarity, decay, search_via in _SMOKE_CANDIDATES
        ],
        record_outcomes=True,
    )

    result = await _runner(temp_db, manager).run(_event(), SESSION_ID, _variables())

    assert result is not None
    injected = [memory["id"] for memory in result.memories]
    assert "database-url-test-protect" in injected
    assert injected == [
        "agent-definition-manager",
        "database-url-test-protect",
        "highest-raw-cosine",
    ]
    assert _drop_row(temp_db, "graph-keyword-synthetic")["drop_detail"] == "selection_min_score"
    assert _drop_row(temp_db, "keyword-only-1")["drop_detail"] == "null_similarity"


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


def test_inline_and_queued_bodies_match() -> None:
    """Rationale is writer provenance; neither delivery route may surface it.

    Both routes read the same `_memory_to_payload` output, so dropping the
    field there is what keeps the queued chunk from saying more about a memory
    than the inline block does.
    """
    rationale = "keep the TS convention for future sessions"
    payload = _memory_to_payload(
        _memory("memory-parity", content="Prefer explicit return types.", rationale=rationale)
    )
    assert "rationale" not in payload

    inline_body = _format_project_memory(payload)

    queued_body = _memory_bodies([payload])[0]
    chunk, _cursor = _next_chunk(
        {
            "recall_request_id": "request-parity",
            "memories": [queued_body],
            "cursor": {"memory_index": 0, "chunk_index": 0},
        }
    )
    queued_memory = chunk["memories"][0]

    assert chunk["final_chunk"] is True, "the whole memory ships in one chunk"
    assert "rationale" not in queued_body
    assert "rationale" not in queued_memory
    assert _format_project_memory(queued_memory) == inline_body
    assert rationale not in inline_body

"""Tests for the digest-pass memory-usefulness judge (#17195)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.config.sessions import MemoryUsefulnessConfig
from gobby.memory.usefulness import (
    PENDING_USEFULNESS_VARIABLE,
    USEFULNESS_PROTOCOL_VERSION,
    USEFULNESS_RUBRIC,
    judge_pending_memory_usefulness,
)
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

SESSION_ID = "66666666-6666-4666-8666-666666666666"


class FakeJudgeLLM:
    def __init__(self, verdicts: list[Any] | None = None):
        self.verdicts = list(verdicts or [])
        self.calls: list[dict[str, Any]] = []

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
        if not self.verdicts:
            return {"useful": True, "confidence": 0.9, "rationale": "used the fact"}
        verdict = self.verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict


class FakeMemoryManager:
    def __init__(self, db: Any, contents: dict[str, str], *, flag: bool = True):
        self.db = db
        self.config = MemoryConfig(digest_memory_usefulness=flag)
        self._contents = contents

    async def aget_memory(self, memory_id: str, project_id: str | None = None, **_: Any) -> Any:
        content = self._contents.get(memory_id)
        if content is None:
            return None
        return SimpleNamespace(id=memory_id, content=content)


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        memory_usefulness=MemoryUsefulnessConfig(candidates=["claude/haiku"]),
    )


def _queue(db: HubDatabase, entries: list[dict[str, Any]]) -> None:
    SessionVariableManager(db).set_variable(SESSION_ID, PENDING_USEFULNESS_VARIABLE, entries)


def _pending(db: HubDatabase) -> Any:
    return SessionVariableManager(db).get_variables(SESSION_ID).get(PENDING_USEFULNESS_VARIABLE)


def _entry(memory_ids: list[str], request_id: str = "req-1") -> dict[str, Any]:
    return {
        "recall_request_id": request_id,
        "memory_ids": memory_ids,
        "project_id": "proj-1",
        "caller": "memory.recall",
    }


async def test_default_off_returns_none_and_preserves_queue(temp_db: HubDatabase) -> None:
    _queue(temp_db, [_entry(["m1"])])
    manager = FakeMemoryManager(temp_db, {"m1": "content"}, flag=False)
    llm = FakeJudgeLLM()

    result = await judge_pending_memory_usefulness(
        memory_manager=manager,
        llm_service=llm,
        config=_config(),
        session_id=SESSION_ID,
        undigested_pairs=[("prompt", "response")],
    )

    assert result is None
    assert llm.calls == []
    # Default-off must not consume the queue.
    assert _pending(temp_db) == [_entry(["m1"])]


async def test_judges_persists_labels_and_clears_queue(temp_db: HubDatabase) -> None:
    _queue(temp_db, [_entry(["m1", "m2"])])
    manager = FakeMemoryManager(temp_db, {"m1": "gcode is the code index", "m2": "other fact"})
    llm = FakeJudgeLLM(
        [
            {"useful": True, "confidence": 0.9, "rationale": "named the exact tool"},
            {"useful": False, "confidence": 0.8, "rationale": "unrelated"},
        ]
    )

    result = await judge_pending_memory_usefulness(
        memory_manager=manager,
        llm_service=llm,
        config=_config(),
        session_id=SESSION_ID,
        undigested_pairs=[("please search with the index", "used gcode search")],
    )

    assert result == [
        {"memory_id": "m1", "helped": True, "rationale": "named the exact tool"},
        {"memory_id": "m2", "helped": False, "rationale": "unrelated"},
    ]
    # Queue consumed exactly once.
    assert _pending(temp_db) == []

    # Judge protocol: rubric system prompt, [TARGET] marker, both siblings shown.
    assert len(llm.calls) == 2
    assert llm.calls[0]["system_prompt"] == USEFULNESS_RUBRIC
    assert llm.calls[0]["caller"] == "memory.usefulness"
    assert "[TARGET]" in llm.calls[0]["prompt"]
    assert "gcode is the code index" in llm.calls[0]["prompt"]
    assert "other fact" in llm.calls[0]["prompt"]

    rows = temp_db.fetchall(
        "SELECT * FROM recall_usefulness WHERE recall_request_id = %s ORDER BY memory_id",
        ("req-1",),
    )
    assert [(r["memory_id"], r["judge_useful"]) for r in rows] == [("m1", True), ("m2", False)]
    assert all(r["label_source"] == "digest" for r in rows)
    assert all(r["judge_protocol_version"] == USEFULNESS_PROTOCOL_VERSION for r in rows)
    assert all(r["position_randomized"] is True for r in rows)
    assert all(r["length_controlled"] is True for r in rows)
    assert all(r["judge_model"] == "claude/haiku" for r in rows)
    assert all(r["session_id"] == SESSION_ID for r in rows)
    assert all(r["project_id"] == "proj-1" for r in rows)


async def test_invalid_verdict_and_judge_error_fail_open(temp_db: HubDatabase) -> None:
    _queue(temp_db, [_entry(["m1", "m2", "m3"])])
    manager = FakeMemoryManager(temp_db, {"m1": "a", "m2": "b", "m3": "c"})
    llm = FakeJudgeLLM(
        [
            {"useful": "yes"},  # invalid: non-bool
            RuntimeError("judge model down"),
            {"useful": True, "confidence": 1.0, "rationale": "ok"},
        ]
    )

    result = await judge_pending_memory_usefulness(
        memory_manager=manager,
        llm_service=llm,
        config=_config(),
        session_id=SESSION_ID,
        undigested_pairs=[("p", "r")],
    )

    assert result == [{"memory_id": "m3", "helped": True, "rationale": "ok"}]
    count = temp_db.fetchone("SELECT count(*) AS n FROM recall_usefulness")
    assert count is not None and count["n"] == 1


async def test_missing_memory_content_skipped(temp_db: HubDatabase) -> None:
    _queue(temp_db, [_entry(["m-gone", "m1"])])
    manager = FakeMemoryManager(temp_db, {"m1": "present"})
    llm = FakeJudgeLLM()

    result = await judge_pending_memory_usefulness(
        memory_manager=manager,
        llm_service=llm,
        config=_config(),
        session_id=SESSION_ID,
        undigested_pairs=[("p", "r")],
    )

    assert result == [{"memory_id": "m1", "helped": True, "rationale": "used the fact"}]
    assert len(llm.calls) == 1


async def test_judged_memory_cap(temp_db: HubDatabase) -> None:
    memory_ids = [f"m{i}" for i in range(12)]
    _queue(temp_db, [_entry(memory_ids)])
    manager = FakeMemoryManager(temp_db, {mid: f"content {mid}" for mid in memory_ids})
    llm = FakeJudgeLLM()

    result = await judge_pending_memory_usefulness(
        memory_manager=manager,
        llm_service=llm,
        config=_config(),
        session_id=SESSION_ID,
        undigested_pairs=[("p", "r")],
    )

    assert result is not None
    assert len(result) == 8
    assert len(llm.calls) == 8


async def test_empty_queue_returns_none(temp_db: HubDatabase) -> None:
    manager = FakeMemoryManager(temp_db, {})
    llm = FakeJudgeLLM()

    result = await judge_pending_memory_usefulness(
        memory_manager=manager,
        llm_service=llm,
        config=_config(),
        session_id=SESSION_ID,
        undigested_pairs=[("p", "r")],
    )

    assert result is None
    assert llm.calls == []


class _FakeSession:
    transcript_path = None
    digest_markdown = ""
    last_digest_input_hash = None
    title = "Existing Title"
    title_source = "llm"
    source = "claude"


class _FakeSessionManager:
    def __init__(self) -> None:
        self.session = _FakeSession()
        self.persist_calls: list[dict[str, Any]] = []

    def get(self, session_id: str) -> Any:
        return self.session

    def persist_digest_state(self, session_id: str, **kwargs: Any) -> Any:
        self.persist_calls.append(kwargs)
        return self.session


class _FakeDigestLLM:
    async def call_feature(self, feature_config: Any, prompt: str, **kwargs: Any) -> str:
        return '{"turn_markdown":"User asked; agent answered.","title_candidate":"Real Work Title"}'


async def test_build_turn_and_digest_attaches_memory_usefulness(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest result carries memory_usefulness; digest fields are unchanged."""
    from gobby.config.sessions import DigestConfig
    from gobby.memory import digest as digest_mod

    judged = [{"memory_id": "m1", "helped": True, "rationale": "r"}]
    seen: dict[str, Any] = {}

    async def fake_judge(**kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return judged

    monkeypatch.setattr(digest_mod, "judge_pending_memory_usefulness", fake_judge)

    manager = FakeMemoryManager(temp_db, {})
    manager.config = MemoryConfig(digest_memory_usefulness=True)
    session_manager = _FakeSessionManager()
    config = SimpleNamespace(
        digest=DigestConfig(),
        memory_usefulness=MemoryUsefulnessConfig(),
    )

    result = await digest_mod.build_turn_and_digest(
        memory_manager=manager,
        session_manager=session_manager,
        session_id=SESSION_ID,
        prompt_text="please investigate the failing recall pipeline in this repo",
        llm_service=_FakeDigestLLM(),
        db=temp_db,
        config=config,
    )

    assert result is not None
    assert result["memory_usefulness"] == judged
    assert result["turn_num"] == 1
    # Digest persistence unchanged by the usefulness pass.
    assert len(session_manager.persist_calls) == 1
    assert "Turn 1" in session_manager.persist_calls[0]["digest_markdown"]
    assert seen["session_id"] == SESSION_ID
    assert seen["undigested_pairs"] == [
        ("please investigate the failing recall pipeline in this repo", "")
    ]


async def test_build_turn_and_digest_omits_field_when_judge_inactive(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No memory_usefulness key when the judge is disabled or yields nothing."""
    from gobby.config.sessions import DigestConfig
    from gobby.memory import digest as digest_mod

    async def fake_judge(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(digest_mod, "judge_pending_memory_usefulness", fake_judge)

    manager = FakeMemoryManager(temp_db, {}, flag=False)
    session_manager = _FakeSessionManager()
    config = SimpleNamespace(
        digest=DigestConfig(),
        memory_usefulness=MemoryUsefulnessConfig(),
    )

    result = await digest_mod.build_turn_and_digest(
        memory_manager=manager,
        session_manager=session_manager,
        session_id=SESSION_ID,
        prompt_text="please investigate the failing recall pipeline in this repo",
        llm_service=_FakeDigestLLM(),
        db=temp_db,
        config=config,
    )

    assert result is not None
    assert "memory_usefulness" not in result
    assert result["turn_num"] == 1
    assert len(session_manager.persist_calls) == 1

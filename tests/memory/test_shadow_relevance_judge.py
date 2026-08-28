"""Tests for durable query-relevance shadow judging."""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.memory.generation_schemas import SHADOW_RELEVANCE_SCHEMA
from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.memory.shadow_relevance import (
    SHADOW_PROTOCOL_VERSION,
    SHADOW_RELEVANCE_RUBRIC,
    _build_shadow_prompt,
    judge_shadow_candidate_relevance,
)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class FakeMemoryManager:
    def __init__(self, contents: dict[str, str], *, enabled: bool = True) -> None:
        self.config = SimpleNamespace(shadow_relevance_judging=enabled)
        self.contents = contents

    async def aget_memory(
        self,
        memory_id: str,
        project_id: str | None = None,
        **_: Any,
    ) -> Any:
        del project_id
        content = self.contents.get(memory_id)
        return SimpleNamespace(content=content) if content is not None else None


class FakeShadowStore:
    def __init__(self, requests: list[dict[str, Any]], events: list[tuple[str, str]]) -> None:
        self.requests = requests
        self.events = events
        self.fetch_calls = 0
        self.label_batches: list[tuple[list[dict[str, Any]], dict[str, Any], str]] = []
        self.retryable: list[tuple[str, str]] = []
        self.terminal: list[tuple[str, str]] = []
        self.poll_kwargs: list[dict[str, Any]] = []
        self.claim_kwargs: list[dict[str, Any]] = []

    def fetch_unshadowed_requests(self, *_: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        self.poll_kwargs.append(dict(kwargs))
        return self.requests[: int(kwargs["limit"])]

    def claim_shadow_request(
        self,
        session_id: str,
        recall_request_id: str,
        **kwargs: Any,
    ) -> str:
        del session_id
        self.claim_kwargs.append(dict(kwargs))
        self.events.append(("claim", recall_request_id))
        return f"token-{recall_request_id}"

    def insert_usefulness_labels_atomic(
        self,
        rows: list[dict[str, Any]],
        snapshot: dict[str, Any],
        claim_token: str,
    ) -> bool:
        request_id = str(snapshot["recall_request_id"])
        self.events.append(("insert", request_id))
        self.label_batches.append((rows, snapshot, claim_token))
        return True

    def mark_shadow_claim_retryable(
        self,
        recall_request_id: str,
        *,
        error: str,
        **_: Any,
    ) -> bool:
        self.events.append(("retryable", recall_request_id))
        self.retryable.append((recall_request_id, error))
        return True

    def mark_shadow_claim_terminal(
        self,
        recall_request_id: str,
        *,
        error: str,
        **_: Any,
    ) -> bool:
        self.events.append(("terminal", recall_request_id))
        self.terminal.append((recall_request_id, error))
        return True


class FakeJudgeLLM:
    def __init__(
        self,
        events: list[tuple[str, str]],
        responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.events = events
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def call_json_feature(
        self,
        feature_config: Any,
        prompt: str,
        system_prompt: str | None = None,
        *,
        json_schema: dict[str, Any],
        caller: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        query = prompt.splitlines()[1]
        self.events.append(("llm", query))
        self.calls.append(
            {
                "feature_config": feature_config,
                "prompt": prompt,
                "system_prompt": system_prompt,
                "json_schema": json_schema,
                "caller": caller,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        keys = re.findall(r"^(M\d+):$", prompt, flags=re.MULTILINE)
        return {
            "verdicts": [
                {
                    "key": key,
                    "relevant": index % 2 == 0,
                    "confidence": 0.75,
                    "rationale": f"reason-{key}",
                }
                for index, key in enumerate(keys)
            ]
        }


def _request(request_id: str, memory_ids: list[str], contents: dict[str, str]) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "recall_request_id": request_id,
        "project_id": "project-1",
        "query": f"query {request_id}",
        "hits": [
            {
                "memory_id": memory_id,
                "rank": index + 1,
                "content_hash": _content_hash(contents[memory_id]),
            }
            for index, memory_id in enumerate(memory_ids)
        ],
    }


def _config() -> Any:
    return SimpleNamespace(
        memory_usefulness=SimpleNamespace(
            profile="low",
            candidates=["codex/gpt-5.6-luna", "claude/haiku"],
            timeout=30,
        )
    )


def test_build_shadow_prompt_is_deterministic_and_masks_memory_ids() -> None:
    hits: list[dict[str, Any]] = [
        {"memory_id": "memory-alpha", "content_hash": "hash-alpha"},
        {"memory_id": "memory-beta", "content_hash": "hash-beta"},
        {"memory_id": "memory-gamma", "content_hash": "hash-gamma"},
    ]
    contents = {
        "memory-alpha": "Alpha content",
        "memory-beta": "Beta content",
        "memory-gamma": "Gamma content",
    }

    prompt, presentation = _build_shadow_prompt(
        recall_request_id="request-42",
        query_text="How should recall ranking work?",
        hits=hits,
        contents_by_id=contents,
        judge_model="codex/gpt-5.6-luna",
        judge_config_fingerprint="judge-fingerprint",
    )
    repeated = _build_shadow_prompt(
        recall_request_id="request-42",
        query_text="How should recall ranking work?",
        hits=list(reversed(hits)),
        contents_by_id=contents,
        judge_model="codex/gpt-5.6-luna",
        judge_config_fingerprint="judge-fingerprint",
    )

    assert (prompt, presentation) == repeated
    assert presentation["system_prompt"] == SHADOW_RELEVANCE_RUBRIC
    assert presentation["query_text"] == "How should recall ranking work?"
    assert presentation["presentation_order"] == ["M1", "M2", "M3"]
    assert [item["neutral_key"] for item in presentation["presented"]] == [
        "M1",
        "M2",
        "M3",
    ]
    assert [item["order_index"] for item in presentation["presented"]] == [0, 1, 2]
    assert presentation["judge_model"] == "codex/gpt-5.6-luna"
    assert presentation["judge_config_fingerprint"] == "judge-fingerprint"
    assert presentation["prompt_hash"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert all(hit["memory_id"] not in prompt for hit in hits)


@pytest.mark.asyncio
async def test_shadow_poll_claims_each_request_immediately_before_its_call() -> None:
    contents = {"memory-a": "Alpha", "memory-b": "Beta", "memory-c": "Gamma"}
    events: list[tuple[str, str]] = []
    store = FakeShadowStore(
        [
            _request("request-1", ["memory-a", "memory-b"], contents),
            _request("request-2", ["memory-c"], contents),
        ],
        events,
    )
    llm = FakeJudgeLLM(events)

    completed = await judge_shadow_candidate_relevance(
        memory_manager=FakeMemoryManager(contents),
        llm_service=llm,
        config=_config(),
        session_id="session-1",
        store=store,
    )

    assert completed == 2
    assert [event[0] for event in events] == ["claim", "llm", "insert"] * 2
    assert [event[1] for event in events if event[0] == "claim"] == [
        "request-1",
        "request-2",
    ]
    assert len(store.label_batches) == 2
    assert len(store.label_batches[0][0]) == 2
    assert store.label_batches[0][1]["judge_protocol_version"] == SHADOW_PROTOCOL_VERSION
    assert store.label_batches[0][1]["created_at"] is not None
    assert all(row["label_source"] == "digest_shadow" for row in store.label_batches[0][0])
    assert all(len(call["feature_config"].candidates) == 1 for call in llm.calls)
    assert all(call["json_schema"] == SHADOW_RELEVANCE_SCHEMA for call in llm.calls)
    assert all(call["system_prompt"] == SHADOW_RELEVANCE_RUBRIC for call in llm.calls)
    assert all(call["caller"] == "memory.shadow_relevance" for call in llm.calls)


@pytest.mark.asyncio
async def test_shadow_poll_is_fenced_to_the_v2_query_construction_era() -> None:
    """4.1.9: the v2 judge polls and claims only requests built by the v2 query.

    Without the explicit fence the poller would inherit the legacy filter and
    stamp v2 protocol labels onto pre-cutover retrievals.
    """
    contents = {"memory-a": "Dispatch uses a staged pipeline"}
    events: list[tuple[str, str]] = []
    store = FakeShadowStore([_request("request-1", ["memory-a"], contents)], events)

    await judge_shadow_candidate_relevance(
        memory_manager=FakeMemoryManager(contents),
        llm_service=FakeJudgeLLM(events),
        config=_config(),
        session_id="session-1",
        store=store,
    )

    assert SHADOW_PROTOCOL_VERSION == "digest-shadow-query-relevance-v2"
    for calls in (store.poll_kwargs, store.claim_kwargs):
        assert [call["judge_protocol_version"] for call in calls] == [SHADOW_PROTOCOL_VERSION]
        assert [call["query_construction_version"] for call in calls] == [
            RECALL_QUERY_CONSTRUCTION_VERSION
        ]


@pytest.mark.asyncio
async def test_shadow_poll_marks_content_drift_terminal_without_calling_judge() -> None:
    contents = {"memory-a": "Current content"}
    events: list[tuple[str, str]] = []
    request = _request("request-drift", ["memory-a"], contents)
    request["hits"][0]["content_hash"] = _content_hash("Old content")
    store = FakeShadowStore([request], events)
    llm = FakeJudgeLLM(events)

    completed = await judge_shadow_candidate_relevance(
        memory_manager=FakeMemoryManager(contents),
        llm_service=llm,
        config=_config(),
        session_id="session-1",
        store=store,
    )

    assert completed == 0
    assert store.terminal == [("request-drift", "content_drift")]
    assert llm.calls == []


@pytest.mark.asyncio
async def test_invalid_response_becomes_retryable_and_does_not_stop_pass() -> None:
    contents = {"memory-a": "Alpha", "memory-b": "Beta"}
    events: list[tuple[str, str]] = []
    store = FakeShadowStore(
        [
            _request("request-invalid", ["memory-a"], contents),
            _request("request-valid", ["memory-b"], contents),
        ],
        events,
    )
    llm = FakeJudgeLLM(events, responses=[{"verdicts": [{"key": "M1"}]}])

    completed = await judge_shadow_candidate_relevance(
        memory_manager=FakeMemoryManager(contents),
        llm_service=llm,
        config=_config(),
        session_id="session-1",
        store=store,
    )

    assert completed == 1
    assert store.retryable == [("request-invalid", "invalid_response")]
    assert len(llm.calls) == 2
    assert store.label_batches[0][1]["recall_request_id"] == "request-valid"


@pytest.mark.asyncio
async def test_shadow_poll_is_disabled_without_consuming_durable_rows() -> None:
    events: list[tuple[str, str]] = []
    store = FakeShadowStore([], events)

    completed = await judge_shadow_candidate_relevance(
        memory_manager=FakeMemoryManager({}, enabled=False),
        llm_service=FakeJudgeLLM(events),
        config=_config(),
        session_id="session-1",
        store=store,
    )

    assert completed == 0
    assert store.fetch_calls == 0


@pytest.mark.asyncio
async def test_shadow_poll_caps_each_pass_at_eight_calls() -> None:
    contents = {f"memory-{index}": f"content-{index}" for index in range(9)}
    events: list[tuple[str, str]] = []
    store = FakeShadowStore(
        [_request(f"request-{index}", [f"memory-{index}"], contents) for index in range(9)],
        events,
    )
    llm = FakeJudgeLLM(events)

    completed = await judge_shadow_candidate_relevance(
        memory_manager=FakeMemoryManager(contents),
        llm_service=llm,
        config=_config(),
        session_id="session-1",
        store=store,
    )

    assert completed == 8
    assert len(llm.calls) == 8
    assert len(store.label_batches) == 8

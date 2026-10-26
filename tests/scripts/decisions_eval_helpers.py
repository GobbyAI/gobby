"""Synthetic datasets and a scripted Noul backend for the decisions eval tests (plan 2.1).

Records carry the shadow fields the harness reads. Probabilities live in per-request
``Script`` entries, not in the records, so one record set can replay through backends that
answer differently. ``content_hash`` is set directly so each record lands in a chosen split.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.ai.decisions import DecisionResult, DecisionsUnavailable, NoulAnswer
from gobby.config.ai import DecisionsConfig, DecisionsWireAPI
from scripts.decisions_eval import Backend

DEV = "development"
HOLDOUT = "holdout"
TOKENS_PER_CALL = 1000
MODEL_CLEF = "clef"
RESPONSE_CLEF = "clef-20261025"
IDENTITY_CLEF = f"response:{RESPONSE_CLEF}"
MODEL_JEV = "jev-1"
RESPONSE_JEV = "jev-1-20261025"
IDENTITY_JEV = f"response:{RESPONSE_JEV}"
SECRET_NAME = "OPENROUTER_KEY"
SECRET_VALUE = "sk-test-NEVER-WRITE-7c2e"


class FakeClock:
    """Seconds clock the harness reads; scripted replays advance it, so no test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass(frozen=True)
class Script:
    """Scripted classifier output for one request. ``forward`` None means unavailable."""

    forward: dict[str, float] | None
    reverse: dict[str, float] | None = None
    latency_ms: float = 100.0
    reverse_latency_ms: float | None = None


class ScriptedNoul:
    """Stands in for ``DecisionService``: answers ``noul`` from per-request scripts."""

    def __init__(
        self,
        scripts: Mapping[str, Script],
        clock: FakeClock,
        identity_for: Callable[[int], str | None],
        response_model: str,
    ) -> None:
        self.scripts = scripts
        self.clock = clock
        self.identity_for = identity_for
        self.response_model = response_model
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def noul(
        self,
        consumer: str,
        state: Mapping[str, Any],
        propositions: Mapping[str, str],
        *,
        timeout_seconds: float | None = None,
    ) -> DecisionResult[NoulAnswer]:
        request = str(state["request"])
        order = tuple(propositions)
        script = self.scripts[request]
        reversed_call = script.forward is not None and order != tuple(script.forward)
        latency_ms = script.latency_ms
        if reversed_call and script.reverse_latency_ms is not None:
            latency_ms = script.reverse_latency_ms
        self.clock.now += latency_ms / 1000
        self.calls.append((request, order))
        if script.forward is None:
            raise DecisionsUnavailable("timeout", "scripted classifier unavailable")
        scores = script.forward
        if order != tuple(scores):
            scores = script.reverse or scores
        answers = {key: NoulAnswer(probability=scores[key]) for key in propositions}
        identity = self.identity_for(len(self.calls) - 1)
        return DecisionResult(
            answers=answers, backend_identity=identity, response_model=self.response_model
        )

    def estimate_request_tokens(
        self, state: Mapping[str, Any], questions: Mapping[str, str]
    ) -> int:
        return TOKENS_PER_CALL


def decisions_config(model: str, wire_api: DecisionsWireAPI = "workers-ai") -> DecisionsConfig:
    return DecisionsConfig(
        api_base="https://decide.test",
        wire_api=wire_api,
        model=model,
        identity_contract="response_version",
    )


class Dataset:
    """Records plus the scripts that answer them. Ids and hashes are counter-based."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.records: list[dict[str, Any]] = []
        self.scripts: dict[str, Script] = {}
        self.clock = clock or FakeClock()
        self._count = 0

    def _allocate(self, split: str) -> tuple[str, str, str]:
        self._count += 1
        residue = 0 if split == HOLDOUT else 1
        content_hash = f"{5 * self._count + residue:064x}"
        return f"req-{self._count}", f"id-{self._count}", content_hash

    def tool(
        self,
        split: str,
        *,
        gold: Sequence[str],
        incumbent: Sequence[str],
        forward: dict[str, float] | None,
        reverse: dict[str, float] | None = None,
        candidates: Sequence[str] = ("a", "b"),
        k: int = 2,
        latency_ms: float = 100.0,
        reverse_latency_ms: float | None = None,
    ) -> str:
        request, record_id, content_hash = self._allocate(split)
        self.records.append(
            {
                "id": record_id,
                "ts": 0.0,
                "consumer": "tool_rerank",
                "state": {"request": request},
                "propositions": {c: f"tool {c}" for c in candidates},
                "candidates": list(candidates),
                "k": k,
                "incumbent": {"status": "ok", "verdict": list(incumbent)},
                "gold_relevant": list(gold),
                "content_hash": content_hash,
                "audit": False,
            }
        )
        self.scripts[request] = Script(forward, reverse, latency_ms, reverse_latency_ms)
        return request

    def found(
        self,
        split: str,
        *,
        gold_shirk: bool,
        incumbent: bool | None,
        probability: float | None,
        latency_ms: float = 100.0,
        audit: bool = False,
    ) -> str:
        request, record_id, content_hash = self._allocate(split)
        self.records.append(
            {
                "id": record_id,
                "ts": 0.0,
                "consumer": "found_work",
                "state": {"request": request},
                "propositions": {"found_work": f"found work {request}"},
                "incumbent": {"status": "ok", "verdict": incumbent},
                "gold_shirk": gold_shirk,
                "content_hash": content_hash,
                "audit": audit,
            }
        )
        forward = None if probability is None else {"found_work": probability}
        self.scripts[request] = Script(forward, None, latency_ms)
        return request

    def duplicate(self, request: str) -> None:
        """Append a second record with identical content (same hash and script)."""
        source = next(r for r in self.records if r["state"]["request"] == request)
        self.records.append({**source, "id": f"{source['id']}-copy"})

    def service(
        self,
        *,
        identity: str | None = IDENTITY_CLEF,
        response_model: str = RESPONSE_CLEF,
        identity_for: Callable[[int], str | None] | None = None,
    ) -> ScriptedNoul:
        resolve = identity_for or (lambda _index: identity)
        return ScriptedNoul(self.scripts, self.clock, resolve, response_model)

    def backend(
        self,
        label: str,
        model: str = MODEL_CLEF,
        wire_api: DecisionsWireAPI = "workers-ai",
        **service_kwargs: Any,
    ) -> Backend:
        return Backend(
            label=label,
            config=decisions_config(model, wire_api),
            service=self.service(**service_kwargs),
        )


def build_tool(
    ds: Dataset,
    split: str,
    *,
    gold: int = 25,
    empty: int = 25,
    empty_a: float = 0.05,
    misses: int = 0,
    flips: int = 0,
    latency_ms: float = 100.0,
) -> None:
    """Gold rows rank ``a`` first (0.95) and gold is ``a``; empty rows score 0.05 unless set.

    ``misses`` gold rows rank ``b`` first; ``flips`` gold rows rank ``b`` first only when
    replayed in reversed order.
    """
    for _ in range(gold - misses - flips):
        ds.tool(
            split,
            gold=["a"],
            incumbent=["a", "b"],
            forward={"a": 0.95, "b": 0.05},
            latency_ms=latency_ms,
        )
    for _ in range(misses):
        ds.tool(
            split,
            gold=["a"],
            incumbent=["a", "b"],
            forward={"a": 0.05, "b": 0.95},
            latency_ms=latency_ms,
        )
    for _ in range(flips):
        ds.tool(
            split,
            gold=["a"],
            incumbent=["a", "b"],
            forward={"a": 0.95, "b": 0.05},
            reverse={"a": 0.05, "b": 0.95},
            latency_ms=latency_ms,
        )
    for _ in range(empty):
        ds.tool(
            split,
            gold=[],
            incumbent=["a", "b"],
            forward={"a": empty_a, "b": 0.05},
            latency_ms=latency_ms,
        )


def build_found(
    ds: Dataset,
    split: str,
    *,
    true_confident: int = 25,
    true_clear: int = 0,
    true_escalated: int = 0,
    false_confident: int = 25,
    false_escalated: int = 0,
    false_high: int = 0,
    latency_ms: float = 100.0,
) -> None:
    """Gold-shirk rows score 0.95 (confirm) and gold-clean rows score 0.05 (clear).

    ``true_clear`` rows score 0.05 on gold-shirk rows, which the cascade clears. The
    ``*_escalated`` rows score 0.5 and take the incumbent verdict. ``false_high`` rows score
    0.95 on gold-clean rows.
    """
    for _ in range(true_confident):
        ds.found(split, gold_shirk=True, incumbent=True, probability=0.95, latency_ms=latency_ms)
    for _ in range(true_clear):
        ds.found(split, gold_shirk=True, incumbent=True, probability=0.05, latency_ms=latency_ms)
    for _ in range(true_escalated):
        ds.found(split, gold_shirk=True, incumbent=True, probability=0.5, latency_ms=latency_ms)
    for _ in range(false_confident):
        ds.found(split, gold_shirk=False, incumbent=False, probability=0.05, latency_ms=latency_ms)
    for _ in range(false_escalated):
        ds.found(split, gold_shirk=False, incumbent=False, probability=0.5, latency_ms=latency_ms)
    for _ in range(false_high):
        ds.found(split, gold_shirk=False, incumbent=False, probability=0.95, latency_ms=latency_ms)


def write_jsonl(records: Sequence[Mapping[str, Any]], path: Path) -> Path:
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
    return path

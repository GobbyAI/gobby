"""Daemon-owned LLM selection for memory recall."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.config.feature_base import DEFAULT_PROFILE_CANDIDATES, FeatureProfile
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.memory.synthetic_prompts import synthetic_prompt_reason
from gobby.prompts.loader import PromptLoader
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.config.sessions import MemoryRecallConfig
    from gobby.memory.manager import MemoryManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.memories import Memory

logger = logging.getLogger(__name__)

PARENT_USER_PROMPT_SOURCES = frozenset(
    {
        SessionSource.AGY,
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GEMINI,
        SessionSource.GROK,
        SessionSource.QWEN,
    }
)
KEYWORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_./:-]{2,}")
KEYWORD_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "because",
        "before",
        "between",
        "could",
        "from",
        "have",
        "into",
        "need",
        "please",
        "should",
        "that",
        "their",
        "there",
        "these",
        "this",
        "those",
        "with",
        "would",
    }
)
REVIEW_LESSON_TAG = "review-lesson"
RECALL_SYSTEM_PROMPT = (
    "Select only memories that are directly useful for the user's current turn. "
    "Return strict JSON and no prose."
)
QUERY_SYNTHESIS_SYSTEM_PROMPT = (
    "Write a compact search query for memory recall. Return strict JSON with a query string."
)


@dataclass(frozen=True)
class MemoryRecallResult:
    """Selected memories plus the parent turn freshness token."""

    origin_turn_seq: int
    recall_request_id: str
    memories: list[dict[str, Any]]


@dataclass(frozen=True)
class MemoryRecallPromptDecision:
    """Classified prompt eligibility for daemon-owned memory recall."""

    eligible: bool
    kind: str
    reason: str
    prompt: str
    source: str
    raw_length: int


@dataclass(frozen=True)
class MemoryRecallQuery:
    """Search query derived from a real user prompt."""

    text: str
    kind: str
    latency_ms: float
    timeout_reason: str | None = None


def is_memory_recall_eligible(
    event: HookEvent,
    variables: dict[str, Any],
    config: MemoryRecallConfig | None,
) -> bool:
    """Return whether this hook event is a real parent user turn."""
    return classify_memory_recall_prompt(event, variables, config).eligible


def classify_memory_recall_prompt(
    event: HookEvent,
    variables: dict[str, Any],
    config: MemoryRecallConfig | None,
) -> MemoryRecallPromptDecision:
    """Classify whether this event carries a real parent user prompt."""
    prompt = event.data.get("prompt")
    prompt_text = prompt if isinstance(prompt, str) else ""
    source = _source_value(event.source)

    if config is not None and not config.enabled:
        return _prompt_decision(False, "disabled", "config_disabled", prompt_text, source)
    if event.event_type != HookEventType.BEFORE_AGENT:
        return _prompt_decision(False, "event", "not_before_agent", prompt_text, source)
    if event.source not in PARENT_USER_PROMPT_SOURCES:
        return _prompt_decision(False, "source", "unsupported_source", prompt_text, source)
    if variables.get("is_spawned_agent"):
        return _prompt_decision(False, "spawned_agent", "spawned_agent", prompt_text, source)
    if not event.metadata.get("_platform_session_id"):
        return _prompt_decision(False, "session", "missing_platform_session", prompt_text, source)
    synthetic_reason = _synthetic_prompt_reason(event, prompt_text)
    if synthetic_reason is not None:
        return _prompt_decision(False, "synthetic", synthetic_reason, prompt_text, source)

    if len(prompt_text.split()) < 6:
        return _prompt_decision(False, "empty_or_short", "prompt_too_short", prompt_text, source)

    if not isinstance(variables.get("parent_turn_seq"), int):
        return _prompt_decision(False, "turn", "missing_parent_turn_seq", prompt_text, source)

    return _prompt_decision(True, "real_user", "eligible", prompt_text, source)


class MemoryRecallRunner:
    """Search candidate memories and ask an LLM feature call to select useful ones."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        memory_manager: MemoryManager,
        llm_service: Any | None,
        config: MemoryRecallConfig,
        log: logging.Logger | None = None,
    ) -> None:
        self.db = db
        self.memory_manager = memory_manager
        self.llm_service = llm_service
        self.config = config
        self.logger = log or logger

    async def run(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> MemoryRecallResult | None:
        """Return fresh memory recall results for an eligible parent turn."""
        decision = classify_memory_recall_prompt(event, variables, self.config)
        if not decision.eligible:
            self._log_recall_diagnostic(
                "Memory recall skipped",
                decision=decision,
                session_id=session_id,
                reason=decision.reason,
                event=event,
            )
            return None
        if self.llm_service is None or not hasattr(self.llm_service, "call_json_feature"):
            self._log_recall_diagnostic(
                "Memory recall skipped",
                decision=decision,
                session_id=session_id,
                reason="llm_unavailable",
                event=event,
            )
            return None

        origin_turn_seq = variables["parent_turn_seq"]
        prompt = decision.prompt
        recall_request_id = str(uuid4())

        deadline = time.monotonic() + self.config.timeout
        query = await self._query_for_prompt(prompt, decision, session_id, event, deadline)
        retrieval_start = time.monotonic()
        candidates = await self._search_candidates(
            query.text,
            event.project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
        )
        retrieval_latency_ms = _elapsed_ms(retrieval_start)
        self._log_recall_diagnostic(
            "Memory recall retrieval completed",
            decision=decision,
            session_id=session_id,
            query=query,
            retrieval_latency_ms=retrieval_latency_ms,
            candidate_count=len(candidates),
            event=event,
        )
        candidate_dicts = self._filter_candidates(candidates, session_id)
        if not candidate_dicts:
            self._log_recall_diagnostic(
                "Memory recall skipped",
                decision=decision,
                session_id=session_id,
                query=query,
                retrieval_latency_ms=retrieval_latency_ms,
                reason="no_candidate_memories",
                event=event,
            )
            return None

        selector_start = time.monotonic()
        selected_ids = await self._select_candidate_ids(
            query.text, candidate_dicts, decision, event, deadline
        )
        selector_latency_ms = _elapsed_ms(selector_start)
        self._log_recall_diagnostic(
            "Memory recall selector completed",
            decision=decision,
            session_id=session_id,
            query=query,
            retrieval_latency_ms=retrieval_latency_ms,
            selector_latency_ms=selector_latency_ms,
            selected_count=len(selected_ids),
            event=event,
        )
        if not selected_ids:
            self._log_recall_diagnostic(
                "Memory recall skipped",
                decision=decision,
                session_id=session_id,
                query=query,
                retrieval_latency_ms=retrieval_latency_ms,
                selector_latency_ms=selector_latency_ms,
                reason="no_selected_memories",
                event=event,
            )
            return None

        candidate_by_id = {memory["id"]: memory for memory in candidate_dicts}
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for memory_id in selected_ids:
            if memory_id in seen:
                continue
            memory = candidate_by_id.get(memory_id)
            if memory is None:
                continue
            seen.add(memory_id)
            selected.append(memory)
            if len(selected) >= self.config.selected_limit:
                break

        if not selected:
            self._log_recall_diagnostic(
                "Memory recall skipped",
                decision=decision,
                session_id=session_id,
                query=query,
                retrieval_latency_ms=retrieval_latency_ms,
                selector_latency_ms=selector_latency_ms,
                reason="selected_ids_not_in_candidates",
                event=event,
            )
            return None
        if not self._is_fresh(session_id, origin_turn_seq):
            self._log_recall_diagnostic(
                "Dropping stale memory_recall",
                decision=decision,
                session_id=session_id,
                query=query,
                retrieval_latency_ms=retrieval_latency_ms,
                selector_latency_ms=selector_latency_ms,
                reason="stale_turn",
                origin_turn_seq=origin_turn_seq,
                event=event,
            )
            return None

        return MemoryRecallResult(
            origin_turn_seq=origin_turn_seq,
            recall_request_id=recall_request_id,
            memories=selected,
        )

    async def _query_for_prompt(
        self,
        prompt: str,
        decision: MemoryRecallPromptDecision,
        session_id: str,
        event: HookEvent,
        deadline: float,
    ) -> MemoryRecallQuery:
        if len(prompt) <= self.config.query_synthesis_threshold:
            return MemoryRecallQuery(text=prompt, kind="original", latency_ms=0.0)

        start = time.monotonic()
        timeout = max(0.0, deadline - start)
        if timeout <= 0:
            return MemoryRecallQuery(
                text=_fallback_query(prompt, self.config.query_max_chars),
                kind="fallback_keywords",
                latency_ms=0.0,
                timeout_reason="query_synthesis_timeout",
            )
        try:
            response = await asyncio.wait_for(
                self._call_query_synthesis_feature(prompt),
                timeout=timeout,
            )
            query = _parse_synthesized_query(response, self.config.query_max_chars)
            return MemoryRecallQuery(text=query, kind="synthesized", latency_ms=_elapsed_ms(start))
        except TimeoutError:
            latency_ms = _elapsed_ms(start)
            self._log_recall_diagnostic(
                "Memory recall query synthesis timed out",
                decision=decision,
                session_id=session_id,
                query=MemoryRecallQuery(
                    text="",
                    kind="synthesis_timeout",
                    latency_ms=latency_ms,
                    timeout_reason="query_synthesis_timeout",
                ),
                reason="query_synthesis_timeout",
                event=event,
            )
            return MemoryRecallQuery(
                text=_fallback_query(prompt, self.config.query_max_chars),
                kind="fallback_keywords",
                latency_ms=latency_ms,
                timeout_reason="query_synthesis_timeout",
            )
        except Exception as exc:  # noqa: BLE001 - recall must fail open
            latency_ms = _elapsed_ms(start)
            self.logger.warning("Memory recall query synthesis failed: %s", exc)
            return MemoryRecallQuery(
                text=_fallback_query(prompt, self.config.query_max_chars),
                kind="fallback_keywords",
                latency_ms=latency_ms,
            )

    async def _search_candidates(
        self,
        prompt: str,
        project_id: str | None,
        *,
        session_id: str,
        recall_request_id: str,
    ) -> list[Memory]:
        try:
            return await self.memory_manager.search_memories(
                query=prompt,
                project_id=project_id,
                limit=self.config.candidate_limit,
                min_score=self.config.min_score,
                tags_none=[REVIEW_LESSON_TAG],
                session_id=session_id,
                recall_request_id=recall_request_id,
                caller="memory.recall",
            )
        except Exception as exc:  # noqa: BLE001 - hook recall must fail open
            self.logger.warning("Memory recall candidate search failed: %s", exc)
            return []

    def _filter_candidates(
        self,
        candidates: list[Memory],
        session_id: str,
    ) -> list[dict[str, Any]]:
        injected = self._injected_memory_ids(session_id)
        seen: set[str] = set()
        filtered: list[dict[str, Any]] = []
        for memory in candidates:
            similarity = getattr(memory, "similarity", None)
            if not isinstance(similarity, int | float) or isinstance(similarity, bool):
                continue
            score = float(similarity)
            if not math.isfinite(score) or score < self.config.min_score:
                continue

            memory_id = getattr(memory, "id", None)
            if not isinstance(memory_id, str) or not memory_id:
                continue
            if _has_review_lesson_tag(getattr(memory, "tags", None)):
                continue
            if memory_id in seen or memory_id in injected:
                continue
            seen.add(memory_id)
            filtered.append(_memory_to_payload(memory))
        return filtered

    async def _select_candidate_ids(
        self,
        prompt: str,
        candidates: list[dict[str, Any]],
        decision: MemoryRecallPromptDecision,
        event: HookEvent,
        deadline: float,
    ) -> list[str]:
        if self.llm_service is None:
            self.logger.debug("Memory recall skipped: LLM service unavailable")
            return []

        recall_prompt = self._render_prompt(prompt, candidates)
        timeout = max(0.0, deadline - time.monotonic())
        if timeout <= 0:
            self._log_recall_diagnostic(
                "Memory recall LLM call timed out",
                decision=decision,
                session_id=str(event.metadata.get("_platform_session_id") or ""),
                reason="selection_timeout",
                event=event,
            )
            return []
        try:
            response = await asyncio.wait_for(
                self._call_selection_feature(recall_prompt),
                timeout=timeout,
            )
        except TimeoutError:
            self._log_recall_diagnostic(
                "Memory recall LLM call timed out",
                decision=decision,
                session_id=str(event.metadata.get("_platform_session_id") or ""),
                reason="selection_timeout",
                event=event,
            )
            return []
        except Exception as exc:  # noqa: BLE001 - hook recall must fail open
            self.logger.warning("Memory recall LLM call failed: %s", exc)
            return []

        try:
            return _parse_selected_memory_ids(response)
        except ValueError as exc:
            self.logger.warning("Memory recall LLM returned invalid JSON: %s", exc)
            return []

    async def _call_selection_feature(self, recall_prompt: str) -> Any:
        llm_service = self.llm_service
        if llm_service is None:
            raise RuntimeError("LLM service unavailable")

        call_json_feature = getattr(llm_service, "call_json_feature", None)
        if not callable(call_json_feature):
            raise RuntimeError("LLM service unavailable")

        return await call_json_feature(
            self.config,
            recall_prompt,
            system_prompt=RECALL_SYSTEM_PROMPT,
            caller="memory.recall",
        )

    async def _call_query_synthesis_feature(self, prompt: str) -> Any:
        llm_service = self.llm_service
        if llm_service is None:
            raise RuntimeError("LLM service unavailable")

        call_json_feature = getattr(llm_service, "call_json_feature", None)
        if not callable(call_json_feature):
            raise RuntimeError("LLM service unavailable")

        return await call_json_feature(
            self._query_synthesis_config(),
            self._render_query_prompt(prompt),
            system_prompt=QUERY_SYNTHESIS_SYSTEM_PROMPT,
            caller="memory.recall.query",
        )

    def _query_synthesis_config(self) -> MemoryRecallConfig:
        return self.config.model_copy(
            update={
                "profile": FeatureProfile.LOW,
                "candidates": list(DEFAULT_PROFILE_CANDIDATES[FeatureProfile.LOW]),
            }
        )

    def _render_query_prompt(self, prompt: str) -> str:
        variables = {
            "user_prompt": prompt,
            "max_query_chars": self.config.query_max_chars,
        }
        try:
            return PromptLoader(db=self.db).render("memory/recall_query_synthesize", variables)
        except Exception as exc:  # noqa: BLE001 - fallback keeps recall available
            self.logger.debug("Falling back to built-in memory recall query prompt: %s", exc)
            return (
                "Condense this user prompt into one memory search query. "
                f"Keep it under {self.config.query_max_chars} characters. "
                'Return strict JSON only: {"query":"..."}\n\n'
                f"User prompt:\n{prompt}"
            )

    def _render_prompt(self, prompt: str, candidates: list[dict[str, Any]]) -> str:
        variables = {
            "user_prompt": prompt,
            "selected_limit": self.config.selected_limit,
            "memories_json": json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
        }
        try:
            return PromptLoader(db=self.db).render("memory/recall_synthesize", variables)
        except Exception as exc:  # noqa: BLE001 - fallback keeps recall available
            self.logger.debug("Falling back to built-in memory recall prompt: %s", exc)
            return (
                "Select directly useful memories for the user prompt.\n"
                f'Return strict JSON: {{"memory_ids":["..."]}} with at most '
                f"{self.config.selected_limit} IDs.\n\n"
                f"User prompt:\n{prompt}\n\nCandidate memories:\n{variables['memories_json']}"
            )

    def _injected_memory_ids(self, session_id: str) -> set[str]:
        try:
            variables = SessionVariableManager(self.db).get_variables(session_id)
        except Exception as exc:  # noqa: BLE001 - recall can still proceed without pre-filter
            self.logger.debug("Failed to read injected_memory_ids for recall: %s", exc)
            return set()
        injected = variables.get("injected_memory_ids")
        if not isinstance(injected, list):
            return set()
        return {item for item in injected if isinstance(item, str)}

    def _is_fresh(self, session_id: str, origin_turn_seq: int) -> bool:
        try:
            variables = SessionVariableManager(self.db).get_variables(session_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Dropping memory_recall: failed freshness lookup: %s", exc)
            return False
        return variables.get("parent_turn_seq") == origin_turn_seq

    def _log_recall_diagnostic(
        self,
        message: str,
        *,
        decision: MemoryRecallPromptDecision,
        session_id: str,
        event: HookEvent,
        query: MemoryRecallQuery | None = None,
        reason: str | None = None,
        retrieval_latency_ms: float | None = None,
        selector_latency_ms: float | None = None,
        candidate_count: int | None = None,
        selected_count: int | None = None,
        origin_turn_seq: int | None = None,
    ) -> None:
        self.logger.debug(
            (
                "%s: prompt_kind=%s source=%s session=%s raw_len=%d query_kind=%s "
                "query_len=%d retrieval_ms=%s selector_ms=%s timeout_reason=%s "
                "reason=%s candidates=%s selected=%s origin_turn_seq=%s caller_metadata=%s"
            ),
            message,
            decision.kind,
            decision.source,
            session_id,
            decision.raw_length,
            query.kind if query else None,
            len(query.text) if query else 0,
            _rounded_latency(retrieval_latency_ms),
            _rounded_latency(selector_latency_ms),
            query.timeout_reason if query else None,
            reason,
            candidate_count,
            selected_count,
            origin_turn_seq,
            _bounded_caller_metadata(event),
        )


def _prompt_decision(
    eligible: bool,
    kind: str,
    reason: str,
    prompt: str,
    source: str,
) -> MemoryRecallPromptDecision:
    return MemoryRecallPromptDecision(
        eligible=eligible,
        kind=kind,
        reason=reason,
        prompt=prompt,
        source=source,
        raw_length=len(prompt),
    )


def _source_value(source: SessionSource | str) -> str:
    return source.value if isinstance(source, SessionSource) else str(source)


def _synthetic_prompt_reason(event: HookEvent, prompt: str) -> str | None:
    return synthetic_prompt_reason(event.metadata, event.data, prompt)


def _memory_to_payload(memory: Memory) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": memory.id,
        "content": memory.content,
        "type": memory.memory_type,
        "created_at": memory.created_at,
        "tags": memory.tags or [],
    }
    for field in (
        "similarity",
        "search_via",
        "ranking_score",
        "raw_semantic_score",
        "temporal_decay_factor",
        "ranking_mode",
    ):
        value = getattr(memory, field, None)
        if value is not None:
            payload[field] = value
    return payload


def _has_review_lesson_tag(tags: Any) -> bool:
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return False
    return REVIEW_LESSON_TAG in tags


def _parse_selected_memory_ids(response: Any) -> list[str]:
    if not isinstance(response, dict):
        raise ValueError("top-level JSON value must be an object")
    raw_ids = response.get("memory_ids")
    if raw_ids is None:
        raise ValueError("missing memory_ids")
    if not isinstance(raw_ids, list):
        raise ValueError("memory_ids must be a list")

    selected: list[str] = []
    for item in raw_ids:
        if isinstance(item, str) and item:
            selected.append(item)
    return selected


def _parse_synthesized_query(response: Any, max_chars: int) -> str:
    if not isinstance(response, dict):
        raise ValueError("response is not an object")
    query = response.get("query")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    query = _bounded_query(query, max_chars)
    if not query:
        raise ValueError("query must be non-empty")
    return query


def _fallback_query(prompt: str, max_chars: int) -> str:
    keywords: list[str] = []
    seen: set[str] = set()
    for match in KEYWORD_PATTERN.findall(prompt):
        keyword = match.strip(".,:;()[]{}<>").lower()
        if keyword in KEYWORD_STOPWORDS or keyword in seen:
            continue
        seen.add(keyword)
        keywords.append(match)
        if len(keywords) >= 80:
            break
    if len(keywords) >= 4:
        return _bounded_query(" ".join(keywords), max_chars)
    return _bounded_query(prompt, max_chars)


def _bounded_query(text: str, max_chars: int) -> str:
    return " ".join(text.split())[:max_chars].rstrip()


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _rounded_latency(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _bounded_caller_metadata(event: HookEvent) -> dict[str, Any]:
    keys = (
        "_platform_session_id",
        "actor",
        "kind",
        "origin",
        "prompt_kind",
        "prompt_origin",
        "prompt_source",
        "prompt_type",
        "role",
        "session_type",
        "source",
        "synthetic",
        "_synthetic",
        "is_synthetic",
        "type",
    )
    metadata: dict[str, Any] = {}
    for key in keys:
        if key not in event.metadata:
            continue
        value = event.metadata[key]
        if isinstance(value, str) and len(value) > 80:
            value = f"{value[:77]}..."
        metadata[key] = value
    return metadata

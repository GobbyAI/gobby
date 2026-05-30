"""Daemon-owned LLM selection for memory recall."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.prompts.loader import PromptLoader
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.config.sessions import MemoryRecallHelperConfig
    from gobby.memory.manager import MemoryManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.memories import Memory

logger = logging.getLogger(__name__)

WAKE_PROMPT_PREFIX = "Message from Gobby daemon: New activity available."
SYSTEM_ACTIVITY_SOURCES = frozenset({"daemon", "system", "pipeline", "gobby_build", "build"})
RECALL_SYSTEM_PROMPT = (
    "Select only memories that are directly useful for the user's current turn. "
    "Return strict JSON and no prose."
)


@dataclass(frozen=True)
class MemoryRecallPayload:
    """Selected memories plus the parent turn freshness token."""

    origin_turn_seq: int
    memories: list[dict[str, Any]]

    def to_message(self) -> dict[str, Any]:
        """Return the legacy memory_recall message payload shape."""
        return {
            "type": "memory_recall",
            "origin_turn_seq": self.origin_turn_seq,
            "memories": self.memories,
        }


def is_memory_recall_eligible(
    event: HookEvent,
    variables: dict[str, Any],
    config: MemoryRecallHelperConfig | None,
) -> bool:
    """Return whether this hook event is a real parent user turn."""
    if config is not None and not config.enabled:
        return False
    if event.event_type != HookEventType.BEFORE_AGENT:
        return False
    if event.source == SessionSource.PIPELINE:
        return False
    if variables.get("is_spawned_agent"):
        return False
    if not event.metadata.get("_platform_session_id"):
        return False
    if _is_synthetic_or_system_activity(event):
        return False

    prompt = event.data.get("prompt")
    if not isinstance(prompt, str) or len(prompt.split()) < 6:
        return False
    if prompt.strip().startswith(WAKE_PROMPT_PREFIX):
        return False

    return isinstance(variables.get("parent_turn_seq"), int)


class MemoryRecallRunner:
    """Search candidate memories and ask an LLM feature call to select useful ones."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        memory_manager: MemoryManager,
        llm_service: Any | None,
        config: MemoryRecallHelperConfig,
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
    ) -> MemoryRecallPayload | None:
        """Return a fresh memory_recall payload for an eligible parent turn."""
        if not is_memory_recall_eligible(event, variables, self.config):
            return None
        if self.llm_service is None or not hasattr(self.llm_service, "call_json_feature"):
            self.logger.debug("Memory recall skipped: LLM service unavailable")
            return None

        origin_turn_seq = variables["parent_turn_seq"]
        prompt = event.data["prompt"]

        candidates = await self._search_candidates(prompt, event.project_id)
        candidate_dicts = self._filter_candidates(candidates, session_id)
        if not candidate_dicts:
            self.logger.debug("Memory recall skipped: no candidate memories")
            return None

        selected_ids = await self._select_candidate_ids(prompt, candidate_dicts)
        if not selected_ids:
            self.logger.debug("Memory recall skipped: LLM selected no memories")
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
            self.logger.debug("Memory recall skipped: selected IDs did not match candidates")
            return None
        if not self._is_fresh(session_id, origin_turn_seq):
            self.logger.debug(
                "Dropping stale memory_recall: origin=%r no longer current",
                origin_turn_seq,
            )
            return None

        return MemoryRecallPayload(origin_turn_seq=origin_turn_seq, memories=selected)

    async def _search_candidates(self, prompt: str, project_id: str | None) -> list[Memory]:
        try:
            return await self.memory_manager.search_memories(
                query=prompt,
                project_id=project_id,
                limit=self.config.candidate_limit,
                min_score=self.config.min_score,
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
            memory_id = getattr(memory, "id", None)
            if not isinstance(memory_id, str) or not memory_id:
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
    ) -> list[str]:
        if self.llm_service is None:
            self.logger.debug("Memory recall skipped: LLM service unavailable")
            return []

        recall_prompt = self._render_prompt(prompt, candidates)
        try:
            response = await asyncio.wait_for(
                self._call_selection_feature(recall_prompt),
                timeout=self.config.timeout,
            )
        except TimeoutError:
            self.logger.warning("Memory recall LLM call timed out")
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
            caller="memory.recall_helper",
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


def _is_synthetic_or_system_activity(event: HookEvent) -> bool:
    for key in ("synthetic", "_synthetic", "is_synthetic"):
        if event.metadata.get(key) or event.data.get(key):
            return True
    for key in ("source", "origin", "actor"):
        value = event.metadata.get(key) or event.data.get(key)
        if isinstance(value, str) and value.lower() in SYSTEM_ACTIVITY_SOURCES:
            return True
    if event.metadata.get("session_type") == "system":
        return True
    return False


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

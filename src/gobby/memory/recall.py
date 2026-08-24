"""Synchronous parent-prompt memory recall."""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.memory.recall_signal_log import make_injection_outcome_recorder
from gobby.memory.scoring import undecay
from gobby.memory.synthetic_prompts import synthetic_prompt_reason
from gobby.utils.datetime import datetime_to_required_iso
from gobby.utils.injected_context import strip_injected_context
from gobby.workflows.state_manager import SessionVariableManager

if TYPE_CHECKING:
    from gobby.config.sessions import MemoryRecallConfig
    from gobby.memory.manager import MemoryManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.memories import Memory

logger = logging.getLogger(__name__)

MAX_RECALL_MEMORIES = 3
MAX_QUERY_TERMS = 80
MAX_QUERY_CHARS = 1_200

# A scrubbed bag this thin carries too little for an embedding to match, so the
# tail of the previous turn is appended as context. Measured on the term bag
# because that is the signal already logged and already bounded.
RECALL_THIN_QUERY_TERMS = 8
RECALL_DIGEST_TAIL_CHARS = 600

PARENT_USER_PROMPT_SOURCES = frozenset(
    {
        SessionSource.AGY,
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
    }
)

REVIEW_LESSON_TAG = "review-lesson"
"""The tag `review_learning.lessons.build_tags` stamps on every recorded lesson."""

_SHORT_ACKNOWLEDGMENTS = frozenset(
    {
        "ack",
        "approved",
        "awesome",
        "cool",
        "correct",
        "exactly",
        "fine",
        "good",
        "got it",
        "great",
        "k",
        "lgtm",
        "makes sense",
        "nice",
        "ok",
        "okay",
        "perfect",
        "right",
        "sounds good",
        "sure",
        "thanks",
        "thank you",
        "understood",
        "yes",
        "yep",
    }
)
_CONTINUATIONS = frozenset(
    {
        "carry on",
        "continue",
        "go ahead",
        "keep going",
        "please continue",
        "proceed",
        "resume",
    }
)
_WAITS = frozenset({"hold", "hold on", "pause", "stop", "wait", "wait a moment"})
_STATUS_PATTERN = re.compile(
    r"^(?:any\s+)?(?:status(?:\s+update)?|progress|updates?)(?:\s+please)?\??$|"
    r"^(?:what(?:'s| is)|how(?:'s| is))\s+(?:the\s+)?(?:status|progress|it going)\??$|"
    r"^(?:are\s+you|is\s+it)\s+done\??$|^(?:done\s+yet|where\s+are\s+we|"
    r"how\s+far\s+along)\??$",
    re.IGNORECASE,
)
_SKILL_COMMAND_PATTERN = re.compile(
    r"^(?:please\s+)?(?:load|reload|use|install|uninstall|enable|disable)\b.*\bskill\b",
    re.IGNORECASE,
)
_LIFECYCLE_COMMAND_PATTERN = re.compile(
    r"^(?:please\s+)?(?:compact|handoff|resume|start|stop|end|close|pause)\b"
    r".*\b(?:agent|goal|session|task|turn)\b",
    re.IGNORECASE,
)
_PROJECT_MEMORY_OPEN = "<project-memory>"
_PROJECT_MEMORY_CLOSE = "</project-memory>"
_PROJECT_MEMORY_BLOCK_PATTERN = re.compile(
    rf"{re.escape(_PROJECT_MEMORY_OPEN)}.*?(?:{re.escape(_PROJECT_MEMORY_CLOSE)}|\Z)",
    re.DOTALL,
)
_QUERY_TERM_PATTERN = re.compile(
    r"`[^`\r\n]+`"
    r"|(?:[A-Za-z]:)?(?:[./~][^\s,;:(){}\[\]]+|[\w.-]+(?:/[\w.@+-]+)+)"
    r"|--?[A-Za-z][\w-]*"
    r"|[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)+"
    r"|[A-Za-z_][A-Za-z0-9_:-]*"
    r"|\d+(?:\.\d+)+"
)
_MEANINGFUL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "please",
        "that",
        "the",
        "this",
        "to",
        "we",
        "with",
        "you",
    }
)
_TECHNICAL_PATTERN = re.compile(
    r"`[^`]+`|(?:[./~][^\s]+)|--?[A-Za-z][\w-]*|"
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)+\b|"
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\b|"
    r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"
)


class PromptDecisionKind(str, Enum):
    """How a prompt reached its recall eligibility decision."""

    HARD_SKIP = "hard_skip"


@dataclass(frozen=True)
class MemoryRecallResult:
    """Ranked memories selected for the current parent turn."""

    origin_turn_seq: int
    recall_request_id: str
    memories: list[dict[str, Any]]


@dataclass(frozen=True)
class MemoryRecallPromptDecision:
    """Why a prompt was excluded from recall, for the decision log."""

    substantive: bool
    kind: PromptDecisionKind
    reason: str
    prompt: str
    source: str
    raw_length: int


def scrub_memory_recall_query(prompt: str) -> str:
    """Build one bounded search query after considering the complete prompt."""
    terms = [match.group(0) for match in _QUERY_TERM_PATTERN.finditer(prompt)]
    scrubbed = [
        term
        for term in terms
        if _is_technical_term(term) or term.casefold() not in _MEANINGFUL_STOPWORDS
    ]
    if not scrubbed:
        return ""

    last_start = max(0, len(scrubbed) - 20)
    ranked_indices = sorted(
        range(len(scrubbed)),
        key=lambda index: (
            index >= last_start,
            _is_technical_term(scrubbed[index]),
            index,
        ),
        reverse=True,
    )
    selected = set(ranked_indices[:MAX_QUERY_TERMS])

    while selected:
        rendered = " ".join(scrubbed[index] for index in sorted(selected))
        if len(rendered) <= MAX_QUERY_CHARS:
            return rendered
        removable = min(
            selected,
            key=lambda index: (
                index >= last_start,
                _is_technical_term(scrubbed[index]),
                index,
            ),
        )
        selected.remove(removable)

    return _elide_to_max_chars(scrubbed[-1])


def _elide_to_max_chars(text: str) -> str:
    """Bound `text` to `MAX_QUERY_CHARS`, keeping its head and its tail."""
    if len(text) <= MAX_QUERY_CHARS:
        return text
    half = (MAX_QUERY_CHARS - 3) // 2
    return f"{text[:half]}...{text[-half:]}"


def _strip_project_memory_blocks(text: str) -> str:
    """Drop rendered `<project-memory>` blocks from a whole turn or a slice of one.

    Recall-local on purpose: `strip_injected_context` has six production
    consumers that must not inherit a rule about memory delivery. The two
    fragment cases mirror that helper's, because both run on a slice: an
    unterminated block runs to the end, and a closing tag with no opener means
    the slice began inside a block, so everything up to it is memory text.
    """
    close_index = text.find(_PROJECT_MEMORY_CLOSE)
    open_index = text.find(_PROJECT_MEMORY_OPEN)
    if close_index != -1 and (open_index == -1 or close_index < open_index):
        text = text[close_index + len(_PROJECT_MEMORY_CLOSE) :]
    return _PROJECT_MEMORY_BLOCK_PATTERN.sub("", text)


def _digest_tail(last_turn_markdown: str) -> str:
    """The previous turn's bounded tail, with everything Gobby injected removed.

    The slice precedes the strippers, so the enrichment is always the tail of
    the turn as it was written. Cleaning first would let the window reach
    further back whenever the turn carried injected blocks, and the bound is on
    the turn rather than on whatever survives cleaning. Both strippers handle a
    block the slice cut through, so recalled memory text cannot feed back into
    the query that retrieves memories.
    """
    sliced = last_turn_markdown[-RECALL_DIGEST_TAIL_CHARS:]
    return _strip_project_memory_blocks(strip_injected_context(sliced)).strip()


def _build_embed_text(prompt: str, query: str, last_turn_markdown: str | None) -> str:
    """Assemble the natural-language query for the vector side of one recall.

    BM25 keeps the scrubbed term bag; an embedding matches the prompt as
    written. A thin bag earns the previous turn's tail as context.
    """
    text = prompt.strip()
    if len(query.split()) < RECALL_THIN_QUERY_TERMS and last_turn_markdown:
        tail = _digest_tail(last_turn_markdown)
        if tail:
            text = f"{text}\n\n{tail}" if text else tail
    return _elide_to_max_chars(text)


@dataclass(frozen=True)
class RecallSessionState:
    """Everything one recall turn reads from the database, in a single batch.

    `last_turn_markdown` is the digest slice; 2.2 enriches a thin query with its
    tail, and batching it here keeps a turn at one executor round trip.
    """

    injected_memory_ids: frozenset[str]
    last_turn_markdown: str | None


class MemoryRecallRunner:
    """Classify one parent prompt and directly rank one hybrid search."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        memory_manager: MemoryManager,
        config: MemoryRecallConfig,
        log: logging.Logger | None = None,
    ) -> None:
        self.db = db
        self.memory_manager = memory_manager
        self.config = config
        self.logger = log or logger
        memory_config = getattr(memory_manager, "config", None)
        self._outcome_recorder = (
            make_injection_outcome_recorder(memory_config, db)
            if memory_config is not None
            else None
        )

    async def run(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> MemoryRecallResult | None:
        """Return up to three direct ranked results for one parent prompt."""
        hard_skip = _hard_skip_reason(event, variables, self.config)
        if hard_skip is not None:
            self._log_decision(
                _decision(False, PromptDecisionKind.HARD_SKIP, hard_skip, event),
                session_id,
            )
            return None

        query = scrub_memory_recall_query(_prompt_text(event))
        if not query:
            return None

        try:
            state = await self.memory_manager.run_db(self._read_session_state, session_id)
        except Exception as exc:  # noqa: BLE001 - partial state is not a basis for injection
            self.logger.warning(
                "Memory recall session read failed; injecting nothing this turn: %s", exc
            )
            return None

        embed_text = _build_embed_text(_prompt_text(event), query, state.last_turn_markdown)
        recall_request_id = str(uuid4())
        started = time.monotonic()
        candidates = await self._search_once(
            query,
            event.project_id,
            session_id=session_id,
            recall_request_id=recall_request_id,
            embed_text=embed_text,
        )
        selected, drops = self._filter_ranked(candidates, state.injected_memory_ids)
        await self._record_selection_outcomes(
            session_id=session_id,
            recall_request_id=recall_request_id,
            project_id=event.project_id,
            turn_seq=variables["parent_turn_seq"],
            drops=drops,
        )
        self.logger.debug(
            "Memory recall search complete: session=%s recall_request_id=%s "
            "construction=%s query_chars=%d embed_chars=%d candidates=%d "
            "selected=%d latency_ms=%.1f",
            session_id,
            recall_request_id,
            RECALL_QUERY_CONSTRUCTION_VERSION,
            len(query),
            len(embed_text),
            len(candidates),
            len(selected),
            (time.monotonic() - started) * 1_000,
        )
        if not selected:
            return None
        return MemoryRecallResult(
            origin_turn_seq=variables["parent_turn_seq"],
            recall_request_id=recall_request_id,
            memories=selected,
        )

    async def _search_once(
        self,
        query: str,
        project_id: str | None,
        *,
        session_id: str,
        recall_request_id: str,
        embed_text: str,
    ) -> list[Memory]:
        try:
            return await self.memory_manager.search_memories(
                query=query,
                project_id=project_id,
                limit=self.config.candidate_limit,
                min_score=self.config.min_score,
                tags_none=[REVIEW_LESSON_TAG],
                embed_text=embed_text,
                session_id=session_id,
                recall_request_id=recall_request_id,
                caller="memory.recall",
            )
        except Exception as exc:  # noqa: BLE001 - recall must fail open
            self.logger.warning("Memory recall hybrid search failed: %s", exc)
            return []

    @staticmethod
    def _undecayed_score(memory: Memory) -> float | None:
        """The candidate's score with the age penalty divided back out.

        `similarity` is `score * user_boost * temporal_decay`, so thresholding
        it made the selection floor a recency test wearing a relevance test's
        name: at a 30-day half-life the decay factor is exactly 0.5, which
        demanded `score * boost >= 1.30` at the old floor -- unreachable at any
        cosine, so every memory aged out of injection on a schedule (#20831).

        Recovered by division rather than read from `raw_semantic_score`,
        because 27.8% of scored hits are graph-synthetic and carry no raw
        cosine; reading it would permanently disable the recall expander
        (#17104). Returns None for a candidate that carries no usable score,
        which is every keyword-only hit.
        """
        similarity = getattr(memory, "similarity", None)
        if (
            not isinstance(similarity, int | float)
            or isinstance(similarity, bool)
            or not math.isfinite(float(similarity))
        ):
            return None
        decay = getattr(memory, "temporal_decay_factor", None)
        if (
            not isinstance(decay, int | float)
            or isinstance(decay, bool)
            or not math.isfinite(float(decay))
        ):
            # No decay was applied that this can divide back out, so the score
            # already is the undecayed one.
            decay = None
        # Shared with the search floor, which reads the same axis (#20858).
        return undecay(float(similarity), None if decay is None else float(decay))

    def _filter_ranked(
        self,
        candidates: list[Memory],
        injected: frozenset[str],
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str, str | None]]]:
        seen: set[str] = set()
        selected: list[dict[str, Any]] = []
        drops: list[tuple[str, str, str | None]] = []
        for memory in candidates:
            memory_id = getattr(memory, "id", None)
            if not isinstance(memory_id, str) or not memory_id or memory_id in seen:
                continue
            seen.add(memory_id)
            if memory_id in injected:
                drops.append((memory_id, "already_injected", None))
                continue
            similarity = self._undecayed_score(memory)
            if similarity is None:
                # An unscored candidate cannot be shown to clear the floor, so it
                # is dropped rather than admitted on the strength of its rank.
                # Keyword-only hits carry no score at all and so are permanently
                # injection-ineligible, which is intended (#20831).
                drops.append((memory_id, "other", "null_similarity"))
                continue
            if similarity < self.config.selection_min_score:
                drops.append((memory_id, "other", "selection_min_score"))
                continue
            if len(selected) < MAX_RECALL_MEMORIES:
                selected.append(_memory_to_payload(memory))
            else:
                drops.append((memory_id, "other", "rank_limit"))
        return selected, drops

    def _read_session_state(self, session_id: str) -> RecallSessionState:
        """Read the dedupe ledger and the digest slice in one executor round trip.

        Runs on the database executor, never on the daemon loop. A raise here
        reaches `run` and costs the turn its injection rather than degrading it
        to partial state.
        """
        injected = (
            SessionVariableManager(self.db).get_variables(session_id).get("injected_memory_ids")
        )
        row = self.db.fetchone(
            "SELECT last_turn_markdown FROM sessions WHERE id = %s",
            (session_id,),
        )
        last_turn = row["last_turn_markdown"] if row is not None else None
        return RecallSessionState(
            injected_memory_ids=frozenset(
                value for value in injected if isinstance(value, str) and value
            )
            if isinstance(injected, list)
            else frozenset(),
            last_turn_markdown=last_turn if isinstance(last_turn, str) else None,
        )

    async def _record_selection_outcomes(
        self,
        *,
        session_id: str,
        recall_request_id: str,
        project_id: str | None,
        turn_seq: int,
        drops: list[tuple[str, str, str | None]],
    ) -> None:
        if self._outcome_recorder is None or not drops:
            return
        rows = [
            {
                "session_id": session_id,
                "recall_request_id": recall_request_id,
                "memory_id": memory_id,
                "project_id": project_id,
                "outcome": "filtered",
                "drop_reason": drop_reason,
                "drop_detail": drop_detail,
                "turn_seq": turn_seq,
                "caller": "memory.recall",
            }
            for memory_id, drop_reason, drop_detail in drops
        ]
        try:
            await self.memory_manager.run_db(self._outcome_recorder, rows)
        except Exception:  # noqa: BLE001 - diagnostics must fail open
            self.logger.debug("Failed to record recall selection outcomes", exc_info=True)

    def _log_decision(
        self,
        decision: MemoryRecallPromptDecision,
        session_id: str,
    ) -> None:
        self.logger.debug(
            "Memory recall prompt decision: substantive=%s kind=%s reason=%s "
            "source=%s session=%s raw_len=%d",
            decision.substantive,
            decision.kind.value,
            decision.reason,
            decision.source,
            session_id,
            decision.raw_length,
        )


def _hard_skip_reason(
    event: HookEvent,
    variables: dict[str, Any],
    config: MemoryRecallConfig | None,
) -> str | None:
    prompt = _prompt_text(event)
    if config is not None and not config.enabled:
        return "config_disabled"
    if event.event_type != HookEventType.BEFORE_AGENT:
        return "not_parent_prompt_event"
    if event.source not in PARENT_USER_PROMPT_SOURCES:
        return "unsupported_source"
    if variables.get("is_spawned_agent"):
        return "spawned_agent"
    if not event.metadata.get("_platform_session_id"):
        return "missing_platform_session"
    if not isinstance(variables.get("parent_turn_seq"), int):
        return "missing_parent_turn_seq"
    synthetic_reason = _synthetic_prompt_reason(event, prompt)
    if synthetic_reason is not None:
        return synthetic_reason

    normalized = " ".join(prompt.casefold().split()).strip(" .!?,")
    if not normalized:
        return "empty_prompt"
    if normalized in _SHORT_ACKNOWLEDGMENTS:
        return "acknowledgment"
    if normalized in _CONTINUATIONS:
        return "continuation"
    if normalized in _WAITS:
        return "wait"
    if _STATUS_PATTERN.fullmatch(normalized):
        return "status_question"
    if normalized.startswith("/") or _SKILL_COMMAND_PATTERN.match(normalized):
        return "skill_command"
    if len(normalized.split()) <= 12 and _LIFECYCLE_COMMAND_PATTERN.match(normalized):
        return "lifecycle_command"
    return None


def _decision(
    substantive: bool,
    kind: PromptDecisionKind,
    reason: str,
    event: HookEvent,
) -> MemoryRecallPromptDecision:
    prompt = _prompt_text(event)
    return MemoryRecallPromptDecision(
        substantive=substantive,
        kind=kind,
        reason=reason,
        prompt=prompt,
        source=_source_value(event.source),
        raw_length=len(prompt),
    )


def _prompt_text(event: HookEvent) -> str:
    prompt = event.data.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def _source_value(source: SessionSource | str) -> str:
    return source.value if isinstance(source, SessionSource) else str(source)


def _synthetic_prompt_reason(event: HookEvent, prompt: str) -> str | None:
    return synthetic_prompt_reason(event.metadata, event.data, prompt)


def _is_technical_term(term: str) -> bool:
    return bool(_TECHNICAL_PATTERN.fullmatch(term)) or any(
        marker in term for marker in ("/", "\\", "_", ".", "::", "--")
    )


def _memory_to_payload(memory: Memory) -> dict[str, Any]:
    """Retain body fields for delivery and search fields for diagnostics only.

    `rationale` is writer provenance, not memory text. Both delivery routes read
    this payload, so omitting it here is what keeps the queued route from saying
    more about a memory than the inline block does. `recall_signal` and dream
    read provenance from the row itself.
    """
    payload: dict[str, Any] = {
        "id": memory.id,
        "content": memory.content,
        "memory_type": (
            memory.memory_type.value
            if hasattr(memory.memory_type, "value")
            else str(memory.memory_type)
        ),
        "tags": list(memory.tags or []),
        "created_at": datetime_to_required_iso(memory.created_at),
        "updated_at": datetime_to_required_iso(memory.updated_at),
    }
    for key in ("similarity", "search_via", "ranking_score", "ranking_mode"):
        value = getattr(memory, key, None)
        if value is not None:
            payload[key] = value
    return payload

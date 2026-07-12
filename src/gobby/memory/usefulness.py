"""Digest-pass memory-usefulness judge (#17195).

Forward label collection per docs/contracts/memory-usefulness-label.md §4/§6:
the workflow delivery chain queues each turn's injected memories (with their
recall_request_id join key) in a session variable; the per-turn digest pass
consumes the queue, runs a de-biased judge per memory, and appends
label_source='digest' rows to recall_usefulness.

The judge deliberately reuses the #17193-calibrated protocol shape — one call
per target memory, sibling memories presented in randomized order with a
[TARGET] marker, length-controlled rubric — rather than bundling the judgment
into the turn-record prompt: bundling cannot randomize presentation per target
and would extend the strict turn_record JSON contract the digest retry loop
depends on. Everything here fails open; digest behavior is never disturbed.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Any

from gobby.config.feature_base import candidate_labels
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

# Session variable written by the delivery chain (EffectsMixin) whenever
# injection outcomes are recorded; consumed (and cleared) by the digest pass.
PENDING_USEFULNESS_VARIABLE = "pending_memory_usefulness"

USEFULNESS_PROTOCOL_VERSION = "17195-digest-v1"

# Truncation budgets match the calibrated #17193 judge harness.
_PROMPT_BUDGET = 1200
_CONTENT_BUDGET = 900
_RESPONSE_BUDGET = 2200
_MAX_JUDGED_MEMORIES = 8
_MAX_PENDING_ENTRIES = 8

USEFULNESS_RUBRIC = (
    "You are auditing an AI coding assistant's memory system. On a past turn, "
    'the assistant received a user request plus injected "project memory" notes, '
    "and produced a response. Judge whether ONE SPECIFIC memory (marked [TARGET]) "
    "materially helped the assistant produce this response.\n\n"
    "Rules:\n"
    '- "Useful" means the response uses information from the TARGET memory that is '
    "not already present in the user request: specific facts, file paths, commands, "
    "conventions, decisions, or warnings that show up in the response or clearly "
    "steered it.\n"
    "- Judge causal usefulness for THIS turn only, not the general quality of the "
    "memory.\n"
    "- Ignore length and verbosity everywhere. A long response does not mean the "
    "memory helped; a short one does not mean it did not.\n"
    "- If the response would plausibly be the same without the TARGET memory, "
    "answer false.\n"
    "- Overlap in generic words is NOT usefulness; look for specific transferred "
    "content or a steered decision.\n\n"
    'Return ONLY JSON: {"useful": true|false, "confidence": 0.0-1.0, '
    '"rationale": "<one line>"}'
)


def _trunc(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def _judge_model_label(feature_config: Any) -> str:
    try:
        labels = candidate_labels(getattr(feature_config, "candidates", []) or [])
        if labels:
            return labels[0]
    except Exception:  # noqa: BLE001 - label resolution must not break judging
        logger.debug("Failed to resolve judge model label", exc_info=True)
    profile = getattr(feature_config, "profile", None)
    return f"profile:{profile}" if profile is not None else "unknown"


def _turn_texts(undigested_pairs: list[tuple[str, str]]) -> tuple[str, str]:
    """Collapse the digested exchange window into judged prompt/response text."""
    if len(undigested_pairs) == 1:
        prompt_text, response_text = undigested_pairs[0]
        return _trunc(prompt_text, _PROMPT_BUDGET), _trunc(response_text, _RESPONSE_BUDGET)
    per_prompt = max(1, _PROMPT_BUDGET // len(undigested_pairs))
    per_response = max(1, _RESPONSE_BUDGET // len(undigested_pairs))
    prompt_parts = []
    response_parts = []
    for index, (prompt_text, response_text) in enumerate(undigested_pairs, 1):
        prompt_parts.append(f"[exchange {index}] {_trunc(prompt_text, per_prompt)}")
        response_parts.append(f"[exchange {index}] {_trunc(response_text, per_response)}")
    return "\n".join(prompt_parts), "\n".join(response_parts)


def _consume_pending_entries(db: HubDatabase, session_id: str) -> list[dict[str, Any]]:
    """Read and clear the pending-judgment queue; empty on any failure."""
    from gobby.workflows.state_manager import SessionVariableManager

    try:
        sv_mgr = SessionVariableManager(db)
        raw = sv_mgr.get_variables(session_id).get(PENDING_USEFULNESS_VARIABLE) or []
        entries = [entry for entry in raw if isinstance(entry, dict)]
        if raw:
            sv_mgr.set_variable(session_id, PENDING_USEFULNESS_VARIABLE, [])
        return entries[:_MAX_PENDING_ENTRIES]
    except Exception:  # noqa: BLE001 - usefulness judging must fail open
        logger.debug("Failed to consume pending memory-usefulness queue", exc_info=True)
        return []


def _build_judge_prompt(
    *,
    target_id: str,
    contents_by_id: dict[str, str],
    sibling_ids: list[str],
    prompt_text: str,
    response_text: str,
    shuffle_seed: str,
) -> str:
    entries = list(sibling_ids)
    # Reproducible presentation order for the judge; no security-sensitive randomness.
    random.Random(shuffle_seed).shuffle(entries)  # nosec B311
    memory_lines = []
    for memory_id in entries:
        tag = " [TARGET]" if memory_id == target_id else ""
        memory_lines.append(f"- {_trunc(contents_by_id[memory_id], _CONTENT_BUDGET)}{tag}")
    return (
        f"USER REQUEST (truncated):\n{prompt_text}\n\n"
        "INJECTED MEMORIES (order shuffled; judge the one marked [TARGET]):\n"
        + "\n".join(memory_lines)
        + f"\n\nASSISTANT RESPONSE (truncated):\n{response_text}"
    )


async def judge_pending_memory_usefulness(
    *,
    memory_manager: Any,
    llm_service: Any,
    config: Any,
    session_id: str,
    undigested_pairs: list[tuple[str, str]],
) -> list[dict[str, Any]] | None:
    """Judge queued injected memories for this turn and persist digest labels.

    Returns the ``memory_usefulness`` entries ([{memory_id, helped, rationale}])
    for the digest result payload, or None when disabled, empty, or on failure.
    """
    memory_config = getattr(memory_manager, "config", None)
    if not getattr(memory_config, "digest_memory_usefulness", False):
        return None
    db = getattr(memory_manager, "db", None)
    if db is None or llm_service is None or not hasattr(llm_service, "call_json_feature"):
        return None

    try:
        entries = _consume_pending_entries(db, session_id)
        if not entries:
            return None

        judge_config = getattr(config, "memory_usefulness", None)
        if judge_config is None:
            logger.debug("memory_usefulness feature config unavailable; skipping judge")
            return None
        judge_model = _judge_model_label(judge_config)
        prompt_text, response_text = _turn_texts(undigested_pairs)

        from gobby.storage.recall_signals import RecallSignalStore

        store = RecallSignalStore(db)
        results: list[dict[str, Any]] = []
        judged_count = 0
        for entry in entries:
            recall_request_id = entry.get("recall_request_id")
            memory_ids = [m for m in entry.get("memory_ids") or [] if isinstance(m, str)]
            if not recall_request_id or not memory_ids:
                continue

            contents_by_id: dict[str, str] = {}
            for memory_id in memory_ids:
                memory = await memory_manager.aget_memory(memory_id)
                content = getattr(memory, "content", None)
                if isinstance(content, str) and content.strip():
                    contents_by_id[memory_id] = content

            for memory_id in memory_ids:
                if memory_id not in contents_by_id:
                    continue
                if judged_count >= _MAX_JUDGED_MEMORIES:
                    logger.debug(
                        "Memory-usefulness judge cap reached (%d); dropping remainder",
                        _MAX_JUDGED_MEMORIES,
                    )
                    break
                judged_count += 1
                judge_prompt = _build_judge_prompt(
                    target_id=memory_id,
                    contents_by_id=contents_by_id,
                    sibling_ids=list(contents_by_id),
                    prompt_text=prompt_text,
                    response_text=response_text,
                    shuffle_seed=f"{recall_request_id}:{memory_id}",
                )
                try:
                    verdict = await llm_service.call_json_feature(
                        judge_config,
                        judge_prompt,
                        system_prompt=USEFULNESS_RUBRIC,
                        caller="memory.usefulness",
                    )
                except Exception as exc:  # noqa: BLE001 - judge must fail open
                    logger.debug("Memory-usefulness judge call failed: %s", exc)
                    continue
                if not isinstance(verdict, dict) or not isinstance(verdict.get("useful"), bool):
                    logger.debug(
                        "Memory-usefulness judge returned invalid verdict for %s", memory_id
                    )
                    continue

                helped = verdict["useful"]
                rationale = verdict.get("rationale")
                confidence = verdict.get("confidence")
                store.insert_usefulness_label(
                    {
                        "session_id": session_id,
                        "recall_request_id": recall_request_id,
                        "memory_id": memory_id,
                        "project_id": entry.get("project_id"),
                        "label_source": "digest",
                        "judge_useful": helped,
                        "judge_confidence": confidence,
                        "judge_model": judge_model,
                        "judge_protocol_version": USEFULNESS_PROTOCOL_VERSION,
                        "position_randomized": True,
                        "length_controlled": True,
                        "rationale": rationale if isinstance(rationale, str) else None,
                        "labeled_at": utc_now(),
                    }
                )
                results.append(
                    {
                        "memory_id": memory_id,
                        "helped": helped,
                        "rationale": rationale if isinstance(rationale, str) else None,
                    }
                )

        return results or None
    except Exception:  # noqa: BLE001 - usefulness judging must never break the digest
        logger.warning("Memory-usefulness judging failed; digest unaffected", exc_info=True)
        return None

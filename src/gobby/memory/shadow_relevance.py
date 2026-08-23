"""Durable query-relevance judging for recall shadow candidates."""

from __future__ import annotations

import hashlib
import json
import logging
import random
from collections.abc import Mapping, Sequence
from copy import copy
from dataclasses import asdict, is_dataclass
from typing import Any

from gobby.config.feature_base import candidate_labels
from gobby.memory.generation_schemas import SHADOW_RELEVANCE_SCHEMA
from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.utils.datetime import utc_now

SHADOW_PROTOCOL_VERSION = "digest-shadow-query-relevance-v2"
SHADOW_RELEVANCE_RUBRIC = """You judge whether each candidate memory would help answer the stored user query.

Score only relevance to the query. A relevant memory supplies specific facts, paths, commands,
constraints, or conventions that materially help with the request beyond merely repeating it.
Word overlap alone is insufficient. Ignore candidate length, writing style, and presentation order.
Judge every neutral key independently. Return JSON with one verdict per candidate under `verdicts`.
Each verdict must contain `key`, boolean `relevant`, confidence from 0 to 1, and `rationale`.
"""

_CONTENT_BUDGET = 2_500
_MAX_REQUESTS_PER_PASS = 8

logger = logging.getLogger(__name__)


def _excerpt(content: str) -> str:
    if len(content) <= _CONTENT_BUDGET:
        return content
    return content[:_CONTENT_BUDGET] + "…"


def _build_shadow_prompt(
    *,
    recall_request_id: str,
    query_text: str,
    hits: Sequence[Mapping[str, Any]],
    contents_by_id: Mapping[str, str],
    judge_model: str,
    judge_config_fingerprint: str,
) -> tuple[str, dict[str, Any]]:
    """Build one reproducible, identity-masked comparative judge prompt."""
    ordered_hits = sorted(hits, key=lambda hit: str(hit["memory_id"]))
    random.Random(recall_request_id).shuffle(ordered_hits)  # nosec B311
    presented: list[dict[str, Any]] = []
    prompt_candidates: list[str] = []
    for order_index, hit in enumerate(ordered_hits):
        memory_id = str(hit["memory_id"])
        neutral_key = f"M{order_index + 1}"
        excerpt = _excerpt(contents_by_id[memory_id])
        presented.append(
            {
                "neutral_key": neutral_key,
                "memory_id": memory_id,
                "order_index": order_index,
                "excerpt": excerpt,
                "content_hash": str(hit["content_hash"]),
            }
        )
        prompt_candidates.append(f"{neutral_key}:\n{excerpt}")

    prompt = f"STORED USER QUERY:\n{query_text}\n\nCANDIDATE MEMORIES:\n" + "\n\n".join(
        prompt_candidates
    )
    presentation: dict[str, Any] = {
        "recall_request_id": recall_request_id,
        "label_source": "digest_shadow",
        "judge_protocol_version": SHADOW_PROTOCOL_VERSION,
        "system_prompt": SHADOW_RELEVANCE_RUBRIC,
        "query_text": query_text,
        "presented": presented,
        "presentation_order": [item["neutral_key"] for item in presented],
        "judge_model": judge_model,
        "judge_config_fingerprint": judge_config_fingerprint,
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    return prompt, presentation


def _json_ready(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _json_ready(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _pin_judge_config(feature_config: Any) -> tuple[Any, str, str]:
    """Pin the first configured candidate and fingerprint all generation settings."""
    candidates = list(getattr(feature_config, "candidates", ()) or ())
    if not candidates:
        raise ValueError("shadow relevance judge requires an explicit candidate")
    first_candidate = candidates[0]
    judge_model = candidate_labels([first_candidate])[0]
    model_copy = getattr(feature_config, "model_copy", None)
    if callable(model_copy):
        pinned_config = model_copy(update={"candidates": [first_candidate]})
    else:
        pinned_config = copy(feature_config)
        pinned_config.candidates = [first_candidate]
    fingerprint_payload = {
        "judge_model": judge_model,
        "generation_config": _json_ready(pinned_config),
    }
    encoded = json.dumps(
        fingerprint_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return pinned_config, judge_model, hashlib.sha256(encoded).hexdigest()


def _validate_verdicts(
    response: Any,
    presented: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]] | None:
    if not isinstance(response, Mapping):
        return None
    verdicts = response.get("verdicts")
    if not isinstance(verdicts, list):
        return None
    expected_keys = {str(item["neutral_key"]) for item in presented}
    validated: dict[str, dict[str, Any]] = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            return None
        key = verdict.get("key")
        relevant = verdict.get("relevant")
        confidence = verdict.get("confidence")
        rationale = verdict.get("rationale")
        if (
            not isinstance(key, str)
            or key not in expected_keys
            or key in validated
            or not isinstance(relevant, bool)
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= float(confidence) <= 1
            or not isinstance(rationale, str)
        ):
            return None
        validated[key] = verdict
    return validated if set(validated) == expected_keys else None


async def _current_contents(
    *,
    memory_manager: Any,
    request: Mapping[str, Any],
) -> tuple[dict[str, str] | None, str | None]:
    contents_by_id: dict[str, str] = {}
    seen_ids: set[str] = set()
    project_id = request.get("project_id")
    for hit in request.get("hits") or []:
        if not isinstance(hit, Mapping):
            return None, "invalid_candidate_set"
        memory_id = hit.get("memory_id")
        expected_hash = hit.get("content_hash")
        if (
            not isinstance(memory_id, str)
            or not memory_id
            or memory_id in seen_ids
            or not isinstance(expected_hash, str)
            or not expected_hash
        ):
            return None, "invalid_candidate_set"
        seen_ids.add(memory_id)
        try:
            memory = await memory_manager.aget_memory(memory_id, project_id=project_id)
        except Exception:
            logger.debug("Failed to load shadow candidate %s", memory_id, exc_info=True)
            return None, "memory_load_failed"
        content = getattr(memory, "content", None) if memory is not None else None
        if not isinstance(content, str):
            return None, "memory_deleted"
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash:
            return None, "content_drift"
        contents_by_id[memory_id] = content
    if not contents_by_id:
        return None, "invalid_candidate_set"
    return contents_by_id, None


def _mark_retryable(store: Any, request_id: str, claim_token: str, error: str) -> None:
    store.mark_shadow_claim_retryable(
        request_id,
        label_source="digest_shadow",
        judge_protocol_version=SHADOW_PROTOCOL_VERSION,
        claim_token=claim_token,
        error=error,
    )


def _mark_terminal(store: Any, request_id: str, claim_token: str, error: str) -> None:
    store.mark_shadow_claim_terminal(
        request_id,
        label_source="digest_shadow",
        judge_protocol_version=SHADOW_PROTOCOL_VERSION,
        claim_token=claim_token,
        error=error,
    )


async def _judge_shadow_entries(
    *,
    request: Mapping[str, Any],
    claim_token: str,
    memory_manager: Any,
    llm_service: Any,
    judge_config: Any,
    judge_model: str,
    judge_config_fingerprint: str,
    store: Any,
) -> bool:
    request_id = str(request.get("recall_request_id") or "")
    contents_by_id, content_error = await _current_contents(
        memory_manager=memory_manager,
        request=request,
    )
    if content_error in {"content_drift", "memory_deleted"}:
        _mark_terminal(store, request_id, claim_token, content_error)
        return False
    if contents_by_id is None:
        _mark_retryable(store, request_id, claim_token, content_error or "invalid_candidate_set")
        return False

    prompt, snapshot = _build_shadow_prompt(
        recall_request_id=request_id,
        query_text=str(request.get("query") or ""),
        hits=request.get("hits") or [],
        contents_by_id=contents_by_id,
        judge_model=judge_model,
        judge_config_fingerprint=judge_config_fingerprint,
    )
    try:
        response = await llm_service.call_json_feature(
            judge_config,
            prompt,
            system_prompt=SHADOW_RELEVANCE_RUBRIC,
            json_schema=SHADOW_RELEVANCE_SCHEMA,
            caller="memory.shadow_relevance",
        )
    except Exception as exc:
        logger.debug("Shadow relevance judge call failed: %s", exc)
        _mark_retryable(store, request_id, claim_token, f"judge_error:{type(exc).__name__}")
        return False

    presented = snapshot["presented"]
    verdicts = _validate_verdicts(response, presented)
    if verdicts is None:
        _mark_retryable(store, request_id, claim_token, "invalid_response")
        return False

    created_at = utc_now()
    snapshot["created_at"] = created_at
    rows: list[dict[str, Any]] = []
    for item in presented:
        verdict = verdicts[str(item["neutral_key"])]
        rows.append(
            {
                "project_id": request.get("project_id"),
                "session_id": request.get("session_id"),
                "recall_request_id": request_id,
                "memory_id": item["memory_id"],
                "label_source": "digest_shadow",
                "judge_useful": verdict["relevant"],
                "judge_confidence": float(verdict["confidence"]),
                "judge_model": judge_model,
                "judge_protocol_version": SHADOW_PROTOCOL_VERSION,
                "position_randomized": True,
                "length_controlled": True,
                "rationale": verdict["rationale"],
                "labeled_at": created_at,
            }
        )
    return bool(store.insert_usefulness_labels_atomic(rows, snapshot, claim_token))


async def judge_shadow_candidate_relevance(
    *,
    memory_manager: Any,
    llm_service: Any,
    config: Any,
    session_id: str,
    store: Any | None = None,
) -> int:
    """Judge up to eight durable recall requests for one session."""
    memory_config = getattr(memory_manager, "config", None)
    if not getattr(memory_config, "digest_shadow_usefulness", False):
        return 0
    if llm_service is None or not callable(getattr(llm_service, "call_json_feature", None)):
        return 0
    judge_feature_config = getattr(config, "memory_usefulness", None)
    if judge_feature_config is None:
        return 0
    try:
        pinned_config, judge_model, judge_config_fingerprint = _pin_judge_config(
            judge_feature_config
        )
    except (TypeError, ValueError):
        logger.warning("Shadow relevance judge has no valid pinned candidate", exc_info=True)
        return 0

    if store is None:
        db = getattr(memory_manager, "db", None)
        if db is None:
            return 0
        from gobby.storage.recall_signals import RecallSignalStore

        store = RecallSignalStore(db)

    try:
        requests = store.fetch_unshadowed_requests(
            session_id,
            label_source="digest_shadow",
            judge_protocol_version=SHADOW_PROTOCOL_VERSION,
            query_construction_version=RECALL_QUERY_CONSTRUCTION_VERSION,
            limit=_MAX_REQUESTS_PER_PASS,
        )
        completed = 0
        for request in requests:
            request_id = str(request.get("recall_request_id") or "")
            if not request_id:
                continue
            claim_token = store.claim_shadow_request(
                session_id,
                request_id,
                label_source="digest_shadow",
                judge_protocol_version=SHADOW_PROTOCOL_VERSION,
                query_construction_version=RECALL_QUERY_CONSTRUCTION_VERSION,
            )
            if claim_token is None:
                continue
            completed += await _judge_shadow_entries(
                request=request,
                claim_token=claim_token,
                memory_manager=memory_manager,
                llm_service=llm_service,
                judge_config=pinned_config,
                judge_model=judge_model,
                judge_config_fingerprint=judge_config_fingerprint,
                store=store,
            )
        return completed
    except Exception:
        logger.warning("Shadow relevance judging failed; digest unaffected", exc_info=True)
        return 0

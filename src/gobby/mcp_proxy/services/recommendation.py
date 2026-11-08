"""Recommendation service."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from gobby.ai import decisions_shadow
from gobby.ai.decisions import DecisionsUnavailable, get_decision_service
from gobby.prompts import PromptLoader
from gobby.storage.hub.protocol import HubDatabase

if TYPE_CHECKING:
    from gobby.ai.decisions import DecisionResult, DecisionService, NoulAnswer
    from gobby.config.ai import DecisionEndpointConfig, DecisionsConfig
    from gobby.config.features import RecommendToolsConfig

logger = logging.getLogger("gobby.mcp.server")


def _noul_endpoint(config: DecisionsConfig) -> DecisionEndpointConfig:
    """The Noul endpoint; the default when unrouted, which refuses Noul."""
    return config.endpoint_for("noul") or config


# Search mode type
SearchMode = Literal["llm", "semantic", "hybrid"]

_CONSUMER = "tool_rerank"
# Decision 8 caps one request at 64 propositions.
_MAX_PROPOSITIONS = 64


@dataclass(frozen=True)
class _Outcome:
    """Classifier rerank result: probabilities by candidate id, or the reason it is unavailable."""

    status: Literal["ok", "unavailable"]
    reason: str | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    backend_identity: str | None = None
    response_model: str | None = None
    estimated_tokens: int = 0
    latency_ms: int = 0


def _candidate_id(candidate: Mapping[str, Any]) -> str:
    return f"{candidate['server']}/{candidate['tool']}"


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _propositions(candidates: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """Return propositions keyed positionally, and each key's candidate id.

    Candidate ids contain '/', which Decision 8 rejects in proposition keys.
    """
    propositions: dict[str, str] = {}
    key_to_id: dict[str, str] = {}
    for index, candidate in enumerate(candidates):
        key = f"c{index}"
        candidate_id = _candidate_id(candidate)
        description = candidate.get("reason", "No description")
        propositions[key] = (
            f"Tool `{candidate_id}` (`{description}`) materially applies to the request."
        )
        key_to_id[key] = candidate_id
    return propositions, key_to_id


def _batches(
    service: DecisionService, state: Mapping[str, Any], propositions: Mapping[str, str]
) -> list[dict[str, str]]:
    """Pack propositions in semantic order into greedy batches that each fit the ceiling."""
    batches: list[dict[str, str]] = []
    pending: dict[str, str] = {}
    for key, text in propositions.items():
        single = {key: text}
        if not service.request_fits(state, single):
            raise DecisionsUnavailable("oversize", "a candidate exceeds the request ceiling")
        trial = {**pending, key: text}
        if len(trial) > _MAX_PROPOSITIONS or not service.request_fits(state, trial):
            batches.append(pending)
            pending = single
        else:
            pending = trial
    if pending:
        batches.append(pending)
    return batches


async def _dispatch(
    service: DecisionService,
    config: DecisionsConfig,
    state: Mapping[str, Any],
    batches: list[dict[str, str]],
) -> list[DecisionResult[NoulAnswer]]:
    """Run every batch under one deadline; one failed batch cancels the rest."""
    timeout = _noul_endpoint(config).timeout_seconds
    try:
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as group:
                calls = [
                    group.create_task(
                        service.noul(_CONSUMER, state, batch, timeout_seconds=timeout)
                    )
                    for batch in batches
                ]
    except* DecisionsUnavailable as failures:
        raise failures.exceptions[0] from None
    return [call.result() for call in calls]


async def _classify(
    service: DecisionService,
    config: DecisionsConfig,
    task_description: str,
    propositions: dict[str, str],
    key_to_id: dict[str, str],
) -> _Outcome:
    """Run the classifier rerank. Every failure becomes an unavailable outcome."""
    started = time.monotonic()
    state = {"request": task_description}
    estimated = 0

    def unavailable(reason: str) -> _Outcome:
        return _Outcome(
            "unavailable", reason, estimated_tokens=estimated, latency_ms=_elapsed_ms(started)
        )

    try:
        batches = _batches(service, state, propositions)
        estimated = sum(service.estimate_request_tokens(state, batch) for batch in batches)
        results = await _dispatch(service, config, state, batches)
    except DecisionsUnavailable as error:
        return unavailable(error.reason)
    except TimeoutError:
        return unavailable("timeout")
    except Exception:
        logger.warning("Tool rerank classifier failed", exc_info=True)
        return unavailable("error")
    identities = {result.backend_identity for result in results}
    models = {result.response_model for result in results}
    # Probabilities combine only from one model: a split across batches is unusable.
    if len(identities) > 1 or len(models) > 1:
        return unavailable("identity_mismatch")
    return _Outcome(
        "ok",
        probabilities={
            key_to_id[key]: answer.probability
            for result in results
            for key, answer in result.answers.items()
        },
        backend_identity=identities.pop(),
        response_model=models.pop(),
        estimated_tokens=estimated,
        latency_ms=_elapsed_ms(started),
    )


def _ranked(
    task_description: str,
    candidates: list[dict[str, Any]],
    probabilities: dict[str, float],
    min_probability: float,
    top_k: int,
) -> dict[str, Any]:
    """Enforce ranking: probability order, ties in semantic order, an empty result is valid."""
    kept = [
        (probabilities[_candidate_id(candidate)], index, candidate)
        for index, candidate in enumerate(candidates)
        if probabilities[_candidate_id(candidate)] >= min_probability
    ]
    kept.sort(key=lambda item: (-item[0], item[1]))
    recommendations = [candidate for _, _, candidate in kept][:top_k]
    return {
        "success": True,
        "task": task_description,
        "search_mode": "decide",
        "recommendation": recommendations,
        "recommendations": recommendations,
        "semantic_candidates": len(candidates),
    }


def _verdict_ids(recommendations: list[Any]) -> list[str]:
    return [
        f"{item.get('server')}/{item.get('tool')}"
        for item in recommendations
        if isinstance(item, dict)
    ]


def _record_shadow(
    config: DecisionsConfig,
    task_description: str,
    top_k: int,
    propositions: dict[str, str],
    key_to_id: dict[str, str],
    outcome: _Outcome,
    incumbent: dict[str, Any],
    incumbent_ok: bool,
) -> None:
    decisions_shadow.record(
        _CONSUMER,
        {
            "model": _noul_endpoint(config).model,
            "state": {"request": task_description},
            "propositions": propositions,
            "candidate_keys": {candidate_id: key for key, candidate_id in key_to_id.items()},
            "candidates": list(key_to_id.values()),
            "k": top_k,
            "classifier": {
                "status": outcome.status,
                "reason": outcome.reason,
                "answers": outcome.probabilities if outcome.status == "ok" else None,
            },
            "incumbent": {
                "status": "ok" if incumbent_ok else "unavailable",
                "verdict": _verdict_ids(incumbent["recommendations"]),
            },
            "backend_identity": outcome.backend_identity,
            "response_model": outcome.response_model,
            "wire_api": _noul_endpoint(config).wire_api,
            "endpoint_host": urlparse(_noul_endpoint(config).api_base or "").hostname,
            "latency_ms": outcome.latency_ms,
            "estimated_tokens": outcome.estimated_tokens,
        },
    )


class RecommendationService:
    """Service for recommending tools."""

    def __init__(
        self,
        llm_service: Any,
        mcp_manager: Any,
        db: HubDatabase | None,
        semantic_search: Any | None = None,
        project_id: str | None = None,
        config_resolver: Callable[[], RecommendToolsConfig | None] | None = None,
        llm_service_resolver: Callable[[], Any | None] | None = None,
        decisions_resolver: Callable[[], DecisionsConfig | None] | None = None,
    ):
        self._llm_service = llm_service
        self._llm_service_resolver = llm_service_resolver
        self._mcp_manager = mcp_manager
        self._semantic_search = semantic_search
        self._project_id = project_id
        self._config_resolver = config_resolver
        self._decisions_resolver = decisions_resolver
        self._mismatch_warning_at = 0.0
        self._loader = PromptLoader(db=db) if db is not None else None

    def _get_config(self) -> RecommendToolsConfig:
        """Get config with fallback to defaults."""
        config = self._config_resolver() if self._config_resolver is not None else None
        if config is not None:
            return config
        from gobby.config.features import RecommendToolsConfig

        return RecommendToolsConfig()

    def _get_llm_service(self) -> Any | None:
        if self._llm_service_resolver is not None:
            resolved = self._llm_service_resolver()
            return self._llm_service if resolved is None else resolved
        return self._llm_service

    async def recommend_tools(
        self,
        task_description: str,
        agent_id: str | None = None,
        search_mode: SearchMode = "llm",
        top_k: int = 10,
        min_similarity: float = 0.3,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Recommend tools based on task description.

        Args:
            task_description: Description of what the user wants to do
            agent_id: Optional agent ID for filtering (reserved for future use)
            search_mode: How to search for tools:
                - "llm": Use LLM to recommend (default, original behavior)
                - "semantic": Use embedding similarity search
                - "hybrid": Combine semantic search with LLM ranking
            top_k: Maximum recommendations to return (for semantic/hybrid)
            min_similarity: Minimum similarity threshold (for semantic/hybrid)
            project_id: Project ID for semantic/hybrid search (overrides instance default)

        Returns:
            Dict with recommendations and metadata
        """
        # Use provided project_id or fall back to instance default
        effective_project_id = project_id or self._project_id

        if search_mode == "semantic":
            return await self._recommend_semantic(
                task_description, top_k, min_similarity, effective_project_id
            )
        elif search_mode == "hybrid":
            return await self._recommend_hybrid(
                task_description, top_k, min_similarity, effective_project_id
            )
        else:
            return await self._recommend_llm(task_description, effective_project_id)

    async def _recommend_semantic(
        self, task_description: str, top_k: int, min_similarity: float, project_id: str | None
    ) -> dict[str, Any]:
        """Recommend tools using semantic similarity search."""
        if not self._semantic_search:
            return {
                "success": False,
                "error": "Semantic search not configured",
                "task": task_description,
            }

        if not project_id:
            return {
                "success": False,
                "error": "Project ID not set for semantic search",
                "task": task_description,
            }

        try:
            results = await self._semantic_search.search_tools(
                query=task_description,
                project_id=project_id,
                top_k=top_k,
                min_similarity=min_similarity,
                mcp_manager=self._mcp_manager,
            )

            recommendations = [
                {
                    "server": r.server_name,
                    "tool": r.tool_name,
                    "reason": r.description or "Semantically similar to query",
                    "similarity": round(r.similarity, 4),
                }
                for r in results
            ]

            return {
                "success": True,
                "task": task_description,
                "search_mode": "semantic",
                "recommendation": recommendations,
                "recommendations": recommendations,
                "total_results": len(results),
            }
        except Exception as e:
            logger.error("Semantic search failed: %s", e)
            return {"success": False, "error": str(e), "task": task_description}

    def _decisions_config(self) -> DecisionsConfig | None:
        return self._decisions_resolver() if self._decisions_resolver is not None else None

    def _warn_mismatch(self, endpoint: DecisionEndpointConfig) -> None:
        """Log an evaluated-backend mismatch once per cooldown window (Decision 10).

        The window belongs to the endpoint that serves Noul, the tool rerank type.
        """
        now = time.monotonic()
        if now - self._mismatch_warning_at >= endpoint.failure_cooldown_seconds:
            logger.warning("evaluated_backend_mismatch: tool rerank decision runs as shadow")
            self._mismatch_warning_at = now

    async def _llm_rerank(
        self, task_description: str, top_k: int, semantic_result: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """LLM-rerank the semantic candidates. Returns the result and whether the LLM succeeded."""
        if self._loader is None:
            semantic_result["search_mode"] = "hybrid_fallback"
            return semantic_result, False

        # Use LLM to re-rank and add reasoning
        try:
            config = self._get_config()
            llm_service = self._get_llm_service()
            if llm_service is None:
                raise RuntimeError("LLM service is unavailable")
            candidates = semantic_result["recommendations"]
            candidate_list = "\n".join(
                f"- {c['server']}/{c['tool']}: {c.get('reason', 'No description')}"
                for c in candidates
            )

            prompt_path = config.hybrid_rerank_prompt_path or "features/recommend_hybrid"
            context = {
                "task_description": task_description,
                "candidate_list": candidate_list,
                "top_k": top_k,
            }
            prompt = self._loader.render(prompt_path, context)

            response = await llm_service.call_feature(
                config,
                prompt,
                caller="mcp_proxy.recommendation.hybrid_rerank",
            )

            # Parse LLM response
            if "```json" in response:
                response = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                response = response.split("```")[1].split("```")[0].strip()

            data = json.loads(response)
            recommendations = data.get("recommendations", [])[:top_k]

            result = {
                "success": True,
                "task": task_description,
                "search_mode": "hybrid",
                "recommendation": recommendations,
                "recommendations": recommendations,
                "semantic_candidates": len(candidates),
            }
            return result, True
        except Exception as e:
            logger.warning("Hybrid LLM re-ranking failed, using semantic results: %s", e)
            # Fall back to semantic results
            semantic_result["search_mode"] = "hybrid_fallback"
            return semantic_result, False

    async def _shadow_rerank(
        self,
        service: DecisionService,
        config: DecisionsConfig,
        task_description: str,
        top_k: int,
        semantic_result: dict[str, Any],
        propositions: dict[str, str],
        key_to_id: dict[str, str],
    ) -> dict[str, Any]:
        """The LLM rerank decides; the classifier runs beside it under its own budget."""
        classifier = asyncio.create_task(
            _classify(service, config, task_description, propositions, key_to_id)
        )
        try:
            incumbent, incumbent_ok = await self._llm_rerank(
                task_description, top_k, semantic_result
            )
        finally:
            if not classifier.done():
                classifier.cancel()
            await asyncio.wait({classifier})
        outcome = (
            _Outcome("unavailable", "cancelled") if classifier.cancelled() else classifier.result()
        )
        _record_shadow(
            config,
            task_description,
            top_k,
            propositions,
            key_to_id,
            outcome,
            incumbent,
            incumbent_ok,
        )
        return incumbent

    async def _decide_rerank(
        self,
        config: DecisionsConfig,
        task_description: str,
        top_k: int,
        semantic_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply the tool_rerank mode: shadow records beside the LLM rerank; enforce ranks."""
        policy = config.tool_rerank
        candidates = semantic_result["recommendations"]
        propositions, key_to_id = _propositions(candidates)
        service = get_decision_service(config)
        if policy.mode == "enforce" and policy.evaluated_model == _noul_endpoint(config).model:
            outcome = await _classify(service, config, task_description, propositions, key_to_id)
            if (
                outcome.status == "ok"
                and outcome.backend_identity is not None
                and outcome.backend_identity == policy.evaluated_backend
            ):
                return _ranked(
                    task_description,
                    candidates,
                    outcome.probabilities,
                    policy.min_probability,
                    top_k,
                )
            if outcome.status == "ok":
                self._warn_mismatch(_noul_endpoint(config))
            incumbent, incumbent_ok = await self._llm_rerank(
                task_description, top_k, semantic_result
            )
            _record_shadow(
                config,
                task_description,
                top_k,
                propositions,
                key_to_id,
                outcome,
                incumbent,
                incumbent_ok,
            )
            return incumbent
        if policy.mode == "enforce":
            self._warn_mismatch(_noul_endpoint(config))
        return await self._shadow_rerank(
            service,
            config,
            task_description,
            top_k,
            semantic_result,
            propositions,
            key_to_id,
        )

    async def _recommend_hybrid(
        self, task_description: str, top_k: int, min_similarity: float, project_id: str | None
    ) -> dict[str, Any]:
        """Recommend tools using semantic search + LLM re-ranking."""
        # First get semantic results
        semantic_result = await self._recommend_semantic(
            task_description,
            top_k * 2,
            min_similarity,
            project_id,  # Get more for re-ranking
        )

        if not semantic_result.get("success") or not semantic_result.get("recommendations"):
            # Fall back to pure LLM if semantic fails
            return await self._recommend_llm(task_description, project_id)
        config = self._decisions_config()
        if config is None or config.tool_rerank.mode == "off":
            result, _ = await self._llm_rerank(task_description, top_k, semantic_result)
            return result
        return await self._decide_rerank(config, task_description, top_k, semantic_result)

    async def _recommend_llm(self, task_description: str, project_id: str | None) -> dict[str, Any]:
        """Recommend tools using LLM (original behavior)."""
        if self._loader is None:
            return {
                "success": False,
                "error": "Recommendation prompt storage is unavailable",
                "task": task_description,
            }
        if not project_id:
            return {
                "success": False,
                "error": "Project ID not set for LLM recommendations",
                "task": task_description,
            }
        try:
            config = self._get_config()
            llm_service = self._get_llm_service()
            if llm_service is None:
                raise RuntimeError("LLM service is unavailable")
            available_servers = self._mcp_manager.get_available_servers(project_id=project_id)

            prompt_path = config.llm_prompt_path or "features/recommend_llm"
            context = {
                "task_description": task_description,
                "available_servers": ", ".join(available_servers),
            }
            prompt = self._loader.render(prompt_path, context)

            response = await llm_service.call_feature(
                config,
                prompt,
                caller="mcp_proxy.recommendation.llm",
            )

            try:
                if "```json" in response:
                    response = response.split("```json")[1].split("```")[0].strip()
                elif "```" in response:
                    response = response.split("```")[1].split("```")[0].strip()

                data = json.loads(response)
                recommendations = data.get("recommendations", [])
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                recommendations = []
                logger.warning("Failed to parse LLM recommendation response: %s", e)

            return {
                "success": True,
                "task": task_description,
                "search_mode": "llm",
                "recommendation": recommendations,
                "recommendations": recommendations,
                "available_servers": available_servers,
            }
        except Exception as e:
            logger.error("Error generating recommendations: %s", e)
            return {"success": False, "error": str(e), "task": task_description}

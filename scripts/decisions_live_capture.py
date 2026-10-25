"""Operator-run live capture of every Jev decision shape (community-label-decisions 3.1)."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from gobby.ai.decisions import (
    BoxImage,
    BoxQuestion,
    ChoiceQuestion,
    DecisionService,
    DecisionsUnavailable,
    ScoreQuestion,
)
from gobby.config.ai import DecisionsConfig
from gobby.llm.base import VisionInputError
from gobby.llm.image_payloads import prepare_image_data

PRIVACY = {"zdr": True, "data_collection": "deny"}
_STATE = {"description": "A red square is centered on a white background."}
_PROPOSITION = "The described square is red."


def add_decision_flags(parser: argparse.ArgumentParser) -> None:
    """Shared explicit configuration for capture and study; no live config reads."""
    parser.add_argument(
        "--wire-api", choices=("systemone", "openrouter-decisions", "workers-ai"), required=True
    )
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--identity-contract",
        choices=("model_card", "response_version"),
        default="response_version",
    )
    parser.add_argument("--backend-max-state-tokens", type=int)
    parser.add_argument("--api-key-secret")


def resolve_operator_secret(name: str) -> str:
    """Read a named secret only in the operator-run process."""
    from gobby.storage.hub.runtime import runtime_hub_database
    from gobby.storage.secrets import SecretStore

    with runtime_hub_database(apply_migrations=False) as db:
        key = SecretStore(db).get(name)
    if not key:
        raise ValueError(f"Secret {name!r} is unset")
    return key


def build_decisions_config(
    args: argparse.Namespace, resolve_secret: Callable[[str], str] | None = None
) -> DecisionsConfig:
    """Build a private config snapshot from flags and an optional secret resolver."""
    config = DecisionsConfig(
        wire_api=args.wire_api,
        api_base=args.api_base,
        model=args.model,
        identity_contract=args.identity_contract,
        backend_max_state_tokens=args.backend_max_state_tokens,
        timeout_seconds=30,
    )
    if args.api_key_secret:
        key = (resolve_secret or resolve_operator_secret)(args.api_key_secret)
        config = config.model_copy(update={"api_key": key})
    return config


def _endpoint(config: DecisionsConfig, model: str) -> str:
    root = (config.api_base or "").rstrip("/")
    if config.wire_api == "systemone":
        return f"{root}/v1/systemone"
    if config.wire_api == "openrouter-decisions":
        return f"{root}/alpha/decisions"
    return f"{root}/ai/run/@cf/cloudflare/{model}"


def _usage(payload: dict[str, Any]) -> dict[str, int | float]:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key in {"input_tokens", "output_tokens", "cost"}
        and type(value) in (int, float)
        and math.isfinite(value)
        and value >= 0
    }


async def _probe(
    client: httpx.AsyncClient, config: DecisionsConfig, model: str, chars: int, name: str
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "state": {"text": ("red square " * (chars // 11 + 1))[:chars]},
        "questions": {"probe": {"type": "noul", "instructions": _PROPOSITION}},
    }
    if config.wire_api == "openrouter-decisions":
        body["provider"] = dict(PRIVACY)
    record: dict[str, Any] = {"probe": name, "request_model": model, "state_chars": chars}
    if config.wire_api == "openrouter-decisions":
        record["privacy_verified"] = body["provider"] == PRIVACY
    try:
        response = await client.post(_endpoint(config, model), json=body)
        record["status"] = response.status_code
        if not response.is_success:
            record["finding"] = f"http_status: {response.status_code}"
            return record
        payload = response.json()
        if config.wire_api == "workers-ai" and isinstance(payload, dict):
            payload = payload.get("result")
        if not isinstance(payload, dict):
            record["finding"] = "parse: response is not an object"
            return record
        record["response_model"] = payload.get("model")
        usage = _usage(payload)
        record["usage"] = usage
        tokens = usage.get("input_tokens", 0)
        if tokens > 0:
            record["chars_per_token"] = chars / tokens
    except (httpx.HTTPError, ValueError) as error:
        record["finding"] = f"{type(error).__name__}: probe did not return valid JSON"
    return record


def _observe_exchange(
    config: DecisionsConfig, record: dict[str, Any], response: httpx.Response
) -> None:
    exchange: dict[str, Any] = {"status": response.status_code}
    if config.wire_api == "openrouter-decisions":
        sent = json.loads(response.request.content)
        exchange["privacy_verified"] = sent.get("provider") == PRIVACY
        if not exchange["privacy_verified"]:
            exchange["finding"] = "required OpenRouter privacy flags absent"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if config.wire_api == "workers-ai" and isinstance(payload, dict):
        payload = payload.get("result")
    if isinstance(payload, dict):
        exchange["usage"] = _usage(payload)
    record["exchanges"].append(exchange)


async def _capture(config: DecisionsConfig, args: argparse.Namespace) -> dict[str, Any]:
    shapes: list[dict[str, Any]] = []
    # Services are intentionally independent: one shape's cooldown cannot hide another.
    for shape in ("choice", "noul", "score", "box"):
        record: dict[str, Any] = {
            "shape": shape,
            "request_model": config.model,
            "state": _STATE,
            "exchanges": [],
        }

        service = DecisionService(
            config, exchange_observer=partial(_observe_exchange, config, record)
        )
        try:
            if shape == "choice":
                choice = ChoiceQuestion(
                    instructions="Choose the square's color.",
                    criteria={"red": "The square is red", "blue": "The square is blue"},
                )
                record["questions"] = {"color": {"type": "choice", **asdict(choice)}}
                result = await service.choose("live_capture", _STATE, {"color": choice})
                record["result"] = asdict(result)
            elif shape == "noul":
                record["questions"] = {"red": {"type": "noul", "instructions": _PROPOSITION}}
                result_noul = await service.noul("live_capture", _STATE, {"red": _PROPOSITION})
                record["result"] = asdict(result_noul)
            elif shape == "score":
                question = ScoreQuestion(
                    instructions="Rate how specifically the description identifies the shape.",
                    levels=("vague", "clear", "clear and specific"),
                )
                record["questions"] = {
                    "clarity": {
                        "type": "score",
                        "instructions": question.instructions,
                        "criteria": question.levels,
                    }
                }
                result_score = await service.score("live_capture", _STATE, {"clarity": question})
                record["result"] = asdict(result_score)
            else:
                record["questions"] = {
                    "square": {
                        "type": "box",
                        "instructions": "Locate the red square.",
                        "image": "omitted",
                    }
                }
                _, mime, encoded, _ = await prepare_image_data(str(args.box_image.resolve()))
                if mime not in ("image/png", "image/jpeg"):
                    raise ValueError("Box capture requires PNG or JPEG")
                image = BoxImage(
                    mime_type="image/png" if mime == "image/png" else "image/jpeg",
                    data_base64=encoded,
                )
                result_box = await service.box(
                    "live_capture",
                    _STATE,
                    {"square": BoxQuestion(instructions="Locate the red square.", image=image)},
                )
                record["result"] = asdict(result_box)
        except DecisionsUnavailable as error:
            record["finding"] = {"reason": error.reason, "detail": error.detail}
        except (VisionInputError, ValueError, OSError) as error:
            record["finding"] = {"reason": "image_input", "detail": str(error)}
        shapes.append(record)

    headers = {"authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    probes: list[dict[str, Any]] = []
    model = config.model or ""
    async with httpx.AsyncClient(
        headers=headers, timeout=config.timeout_seconds, trust_env=False, follow_redirects=False
    ) as client:
        for chars in (4096, 8192):
            probes.append(await _probe(client, config, model, chars, "chars-per-token"))
        # Differencing cancels fixed question/envelope overhead in input_tokens.
        measured_ratio: float | None = None
        overhead = 0.0
        first_tokens = probes[0].get("usage", {}).get("input_tokens", 0)
        second_tokens = probes[1].get("usage", {}).get("input_tokens", 0)
        if 0 < first_tokens < second_tokens:
            candidate_ratio = (probes[1]["state_chars"] - probes[0]["state_chars"]) / (
                second_tokens - first_tokens
            )
            if math.isfinite(candidate_ratio):
                measured_ratio = candidate_ratio
                overhead = max(0.0, first_tokens - probes[0]["state_chars"] / measured_ratio)
        ratio = measured_ratio if measured_ratio is not None else 4.0
        limit = config.resolved_backend_max_state_tokens
        for name, multiplier in (("just-under", 0.99), ("over-limit", 1.05)):
            chars = max(0, int((limit * multiplier - overhead) * ratio))
            probes.append(await _probe(client, config, model, chars, name))
        pinned = await _probe(client, config, model, 4096, "pinned-model")
        alias = await _probe(client, config, args.alias, 4096, "alias")
        probes.extend((pinned, alias))
        alias_matches = pinned.get("response_model") is not None and pinned.get(
            "response_model"
        ) == alias.get("response_model")
        canonical: dict[str, Any] = {}
        if config.wire_api == "openrouter-decisions":
            try:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models", params={"output_modalities": "decisions"}
                )
                response.raise_for_status()
                data = response.json().get("data", [])
                canonical = {
                    str(item["id"]): item.get("canonical_slug")
                    for item in data
                    if isinstance(item, dict) and item.get("id") in (model, args.alias)
                }
                if not canonical.get(model):
                    canonical["finding"] = "request model has no canonical_slug in listing"
            except (httpx.HTTPError, ValueError, AttributeError, TypeError):
                canonical = {"finding": "canonical_slug listing unavailable"}
    return {
        "wire_api": config.wire_api,
        "api_base": config.api_base,
        "request_model": config.model,
        "identity_contract": config.identity_contract,
        "backend_max_state_tokens": limit,
        "shapes": shapes,
        "probes": probes,
        "measured_chars_per_token": measured_ratio,
        "limit_probe_chars_per_token": ratio,
        "estimated_input_overhead_tokens": overhead if measured_ratio is not None else None,
        "alias_matches_pinned_response": alias_matches,
        "canonical_slug": canonical,
        "privacy_policy": PRIVACY if config.wire_api == "openrouter-decisions" else None,
    }


def main(
    argv: list[str] | None = None,
    *,
    resolve_secret: Callable[[str], str] | None = None,
) -> int:
    """Capture Choice, Noul, Score and Box round trips plus raw probes into a dated section."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_decision_flags(parser)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--box-image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    config = build_decisions_config(args, resolve_secret)
    capture = asyncio.run(_capture(config, args))
    date = datetime.now(UTC).date().isoformat()
    section = (
        f"## Decision capture {date}\n\n"
        "Service round trips validate all four shapes. Raw probes record usage and cost, "
        "chars-per-token, both input-limit responses and the alias comparison. "
        "A failed shape remains a finding; successful probes do not waive it.\n\n"
        "The measured ratio uses the difference in state characters divided by the difference "
        "in reported input tokens across two probes, removing fixed question overhead. "
        "Limit-probe sizes account for the estimated overhead. If calibration is unavailable, "
        "the measured ratio is null and probes assume 4 characters per token with zero overhead. "
        "The responses establish whether the backend rejects or truncates.\n\n"
        "OpenRouter privacy flags are checked on every service exchange and raw probe. "
        "Input scope, documented errors and usage: "
        "[OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), "
        "[decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), "
        "[TypeSafe models](https://docs.typesafe.ai/models), and "
        "[local wire contracts](wire-contracts.md). Documentation references are those "
        "recorded in the wire contracts; this run measures the backend independently.\n\n"
        f"```json\n{json.dumps(capture, indent=2, ensure_ascii=False)}\n```\n"
    )
    if config.api_key:
        section = section.replace(config.api_key, "[redacted]")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as stream:
        stream.write(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
